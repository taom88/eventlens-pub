"""UI-neutral command, transition, and read boundary for product sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventlens.config import EventLensSettings
from eventlens.product import fixture_replay as fixture_product
from eventlens.product import run as product_run
from eventlens.product.ask import ResearchAskResult, infer_ticker
from eventlens.product.synthesis_input import ResearchEvidenceSource
from eventlens.product.fixture_replay import (
    DEFAULT_SESSION_ID as FIXTURE_SESSION_ID,
    ExternalHandoffState,
    ExternalHandoffStatus,
)
from eventlens.agents.external_handoff import ExternalAgentHandoffStore
from eventlens.providers.errors import ProviderConfigurationError, ProviderError
from eventlens.memory import (
    DurableRunRecord,
    ImmutableRevisionError,
    RunRecordKind,
    canonical_run_record_checksum,
    repository_from_settings,
)
from eventlens.web.handoff import HandoffImportResult, SessionHandoffFacade


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SCHEMA_VERSION = "eventlens.product-session.v1"


class SessionMode(StrEnum):
    """The only source modes exposed by the product façade."""

    FIXTURE_REPLAY = "fixture_replay"
    LIVE = "live"


class ProductSessionError(RuntimeError):
    """Base class for typed façade errors."""

    code = "product_session_error"


class ProductSessionInputError(ValueError, ProductSessionError):
    code = "invalid_product_session_input"


class ProductSessionCollisionError(ProductSessionError):
    """A stable key is already bound to a different request or source mode."""

    code = "session_collision"


class ProductSessionNotFoundError(ProductSessionError):
    code = "session_not_found"


class ProductSessionIntegrityError(ProductSessionError):
    code = "session_integrity_failure"


class ProductSessionStaleRevisionError(ProductSessionIntegrityError):
    """A transition was based on a repository revision that is no longer current."""

    code = "stale_session_revision"


class ProductSessionErrorInfo(BaseModel):
    """Secret-safe, serializable execution failure shown by a UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)
    provider: str | None = None
    retryable: bool = False


class ProviderReadiness(BaseModel):
    """Typed live-provider readiness without credential values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: SessionMode
    ready: bool
    required: tuple[str, ...] = ()
    available: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    detail: str = Field(min_length=1)


class ArtifactReference(BaseModel):
    """A checksum-verified file reference safe for a read-only UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verified: bool = True
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    subresource: str | None = None


class ProductSession(BaseModel):
    """Canonical read/write result for a product session.

    ``status`` and ``stage`` are derived from ``handoff`` whenever a handoff
    exists.  This is intentional: the durable handoff state is authoritative
    over a stale ResearchAskResult status after an explicit report import.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SCHEMA_VERSION
    session_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    question: str = Field(min_length=1)
    mode: SessionMode
    origin: str | None = None
    ticker: str | None = None
    event_id: str | None = None
    event_date: date | None = None
    information_cutoff_at: datetime | None = None
    event_release_at: datetime | None = None
    warnings: tuple[str, ...] = ()
    execution_pid: int | None = Field(default=None, ge=1)
    status: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    readiness: ProviderReadiness
    error: ProductSessionErrorInfo | None = None
    result: ResearchAskResult | None = None
    handoff: ExternalHandoffState | None = None
    artifact_refs: tuple[ArtifactReference, ...] = ()
    context_refs: tuple[ArtifactReference, ...] = ()
    baseline_ref: ArtifactReference | None = None
    handoff_ref: ArtifactReference | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_identity(self) -> "ProductSession":
        _validate_session_id(self.session_id)
        _validate_idempotency_key(self.idempotency_key)
        if self.result is not None and self.result.session_id != self.session_id:
            raise ValueError("product result session binding mismatch")
        if self.handoff is not None and self.handoff.session_id != self.session_id:
            raise ValueError("product handoff session binding mismatch")
        if self.mode is SessionMode.FIXTURE_REPLAY and self.origin != "fixture_replay":
            raise ValueError("fixture replay sessions must have fixture_replay origin")
        return self


class _PersistedProductSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SCHEMA_VERSION
    session: ProductSession


class ProductSessionFacade:
    """Synchronous product service backed by authoritative repository revisions."""

    def __init__(
        self,
        settings: EventLensSettings | None = None,
        *,
        session_root: Path | None = None,
        output_dir: Path | None = None,
        fixture_root: Path | None = None,
        evidence_source: ResearchEvidenceSource | None = None,
    ) -> None:
        if session_root is not None and output_dir is not None:
            raise ValueError("provide session_root or output_dir, not both")
        selected_root = session_root or output_dir
        self.root = Path(selected_root or (settings or EventLensSettings()).workflow_artifact_dir).resolve()
        self.settings = settings or EventLensSettings(
            memory_sqlite_path=self.root / "product-session-memory.sqlite"
        )
        if (
            selected_root is not None
            and self.settings.memory_backend == "local"
            and self.settings.memory_sqlite_path == Path(".eventlens-cache/research-memory.sqlite")
        ):
            self.settings = self.settings.model_copy(
                update={"memory_sqlite_path": self.root / "product-session-memory.sqlite"}
            )
        self.repository = repository_from_settings(self.settings)
        self.sessions_root = self.root / "sessions"
        self.fixture_handoff_root = self.root / "fixture-handoff"
        self.live_artifact_root = self.root / "live-artifacts"
        self.fixture_root = fixture_root
        self.evidence_source = evidence_source

    def submit(
        self,
        question: str,
        mode: SessionMode | str,
        idempotency_key: str | None = None,
        session_id: str | None = None,
        ticker: str | None = None,
    ) -> ProductSession:
        """Start or idempotently return one typed product session.

        Blank and unsupported fixture prompts are input errors.  Expected live
        provider-readiness failures are persisted and returned as a typed
        failed session so a UI can explain the missing setup without catching
        provider implementation exceptions.
        """

        normalized_question = _normalize_question(question)
        selected_mode = _normalize_mode(mode)
        requested_ticker = _normalize_ticker(ticker) if ticker is not None else None
        requested_session = _resolve_requested_session(
            selected_mode, idempotency_key=idempotency_key, session_id=session_id
        )
        requested_key = _resolve_idempotency_key(
            selected_mode, idempotency_key=idempotency_key, session_id=requested_session
        )
        _validate_session_id(requested_session)
        _validate_idempotency_key(requested_key)
        self._reject_reserved_collision(selected_mode, requested_session)

        existing = self._find_existing(
            session_id=requested_session,
            mode=selected_mode,
            question=normalized_question,
            idempotency_key=requested_key,
            ticker=requested_ticker,
        )
        if existing is not None:
            return existing

        collision = self._find_idempotency_collision(requested_key)
        if collision is not None:
            raise ProductSessionCollisionError(
                f"idempotency key is already bound to session {collision.session_id}"
            )

        if selected_mode is SessionMode.FIXTURE_REPLAY:
            session = self._submit_fixture(
                normalized_question, requested_key, requested_session
            )
        else:
            readiness = _live_readiness(self.settings, self.evidence_source)
            if readiness.ready:
                expected_revision = self._persist(
                    self._pending_live_session(
                        normalized_question,
                        requested_key,
                        requested_session,
                        readiness,
                        ticker=requested_ticker,
                    ),
                    expected_revision=0,
                )
            else:
                expected_revision = 0
            session = self._submit_live(
                normalized_question,
                requested_key,
                requested_session,
                ticker=requested_ticker,
            )
        self._persist(session, expected_revision=expected_revision if selected_mode is SessionMode.LIVE else 0)
        return session

    def _pending_live_session(
        self,
        question: str,
        idempotency_key: str,
        session_id: str,
        readiness: ProviderReadiness,
        *,
        ticker: str | None = None,
    ) -> ProductSession:
        return ProductSession(
            session_id=session_id,
            idempotency_key=idempotency_key,
            question=question,
            mode=SessionMode.LIVE,
            ticker=ticker or infer_ticker(question),
            status="running",
            stage="provider_collection",
            readiness=readiness,
            warnings=(
                "A persisted provider-call reservation exists; an interrupted run is not retried automatically.",
            ),
            execution_pid=os.getpid(),
            created_at=datetime.now(timezone.utc),
        )

    def load(self, session_id: str) -> ProductSession:
        """Load and checksum-verify one exact session after process restart."""

        _validate_session_id(session_id)
        run_id = _product_run_id(
            session_id,
            owner_id=self.settings.memory_owner_id,
            workspace_id=self.settings.memory_workspace_id,
        )
        rows = self.repository.list_run_records(
            self.settings.memory_workspace_id, run_id, kind=RunRecordKind.TRANSITION
        )
        if rows:
            latest = max(rows, key=lambda row: row.revision)
            repository_revision = latest.revision
            session_value = latest.metadata.get("product_session")
            if not isinstance(session_value, dict):
                raise ProductSessionIntegrityError("durable product session is missing its typed payload")
            if _sha256_bytes(_canonical_json(session_value)) != latest.metadata.get("product_session_sha256"):
                raise ProductSessionIntegrityError("durable product-session checksum mismatch")
            try:
                session = ProductSession.model_validate(session_value)
            except ValueError as error:
                raise ProductSessionIntegrityError("invalid durable product session") from error
            if session.session_id != session_id:
                raise ProductSessionIntegrityError("durable product-session identity mismatch")
            if (
                session.status == "running"
                and session.execution_pid is not None
                and session.execution_pid != os.getpid()
            ):
                session = session.model_copy(update={
                    "status": "interrupted",
                    "stage": "interrupted_provider_collection",
                    "warnings": (
                        "The prior process ended during provider collection; no provider calls were retried automatically.",
                    ),
                    "execution_pid": None,
                })
                repository_revision = self._persist(
                    session, expected_revision=repository_revision
                )
            self._write_session_mirror(session)
            session = self._merge_handoff(
                session, expected_revision=repository_revision
            )
            self._verify_references(session)
            return session

        if self.settings.memory_backend == "supabase":
            raise ProductSessionNotFoundError(f"product session {session_id!r} was not found")

        path = self.sessions_root / f"{session_id}.json"
        sidecar = path.with_suffix(".json.sha256")
        if not path.is_file() or not sidecar.is_file():
            raise ProductSessionNotFoundError(f"product session {session_id!r} was not found")
        expected = sidecar.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ProductSessionIntegrityError("invalid product-session checksum sidecar")
        actual = _sha256_file(path)
        if expected != actual:
            raise ProductSessionIntegrityError("product-session checksum mismatch")
        try:
            envelope = _PersistedProductSession.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise ProductSessionIntegrityError("invalid persisted product session") from error
        session = envelope.session
        if session.session_id != session_id:
            raise ProductSessionIntegrityError("product-session filename binding mismatch")
        repository_revision = self._persist(session, expected_revision=0)
        if (
            session.status == "running"
            and session.execution_pid is not None
            and session.execution_pid != os.getpid()
        ):
            session = session.model_copy(
                update={
                    "status": "interrupted",
                    "stage": "interrupted_provider_collection",
                    "warnings": (
                        "The prior process ended during provider collection; no provider calls were retried automatically.",
                    ),
                    "execution_pid": None,
                }
            )
            repository_revision = self._persist(
                session, expected_revision=repository_revision
            )
        session = self._merge_handoff(
            session, expected_revision=repository_revision
        )
        self._verify_references(session)
        return session

    def import_report(
        self, session_id: str, raw_report: str | bytes
    ) -> HandoffImportResult:
        """Validate a report and durably advance its canonical session.

        The low-level handoff store owns report validation and immutable files;
        this application service owns the product transition. Success is
        returned only after the configured repository contains the validated
        session revision. If repository persistence fails, the exception is
        propagated and callers must not report a successful import.
        """

        session = self.load(session_id)
        imported = SessionHandoffFacade(
            self._handoff_root(session.mode), settings=self.settings
        ).import_report(session_id, raw_report)
        if not imported.ok:
            return imported
        persisted = self.load(session_id)
        if (
            persisted.handoff is None
            or persisted.handoff.status is not ExternalHandoffStatus.VALIDATED
            or imported.report is None
            or persisted.handoff.report_id != imported.report.report_id
        ):
            raise ProductSessionIntegrityError(
                "validated report was not committed to the product session"
            )
        return imported

    def _submit_fixture(
        self, question: str, idempotency_key: str, session_id: str
    ) -> ProductSession:
        if session_id != FIXTURE_SESSION_ID:
            raise ProductSessionCollisionError(
                f"fixture replay session_id is immutable: {FIXTURE_SESSION_ID}"
            )
        preparation = fixture_product.prepare_d0_demo_fixture_replay(
            self.fixture_handoff_root,
            fixture_root=self.fixture_root,
            session_id=session_id,
        )
        handoff = preparation.exported
        package = preparation.package
        bundle = package.bundle
        ticker = infer_ticker(question)
        if ticker != package.manifest.ticker:
            raise ProductSessionInputError(
                "fixture request ticker does not match the selected fixture instance"
            )
        context_refs = tuple(
            _verified_reference(
                package.root / filename,
                kind="context_card",
                expected_sha256=package.manifest.checksums[filename],
                content_sha256=card.content_sha256,
            )
            for filename, card in zip(package.manifest.card_files, package.cards)
        )
        baseline_path = package.root / package.manifest.baseline_file
        baseline_ref = _verified_reference(
            baseline_path,
            kind="baseline",
            expected_sha256=package.manifest.checksums[package.manifest.baseline_file],
            content_sha256=package.baseline.content_sha256,
        )
        handoff_ref = _handoff_state_reference(self.fixture_handoff_root, handoff)
        refs = (*context_refs, baseline_ref, handoff_ref, _handoff_bundle_reference(handoff))
        session = ProductSession(
            session_id=session_id,
            idempotency_key=idempotency_key,
            question=question,
            mode=SessionMode.FIXTURE_REPLAY,
            origin="fixture_replay",
            ticker=ticker,
            event_id=bundle.event_id,
            event_date=(
                bundle.target_release_at.date()
                if bundle.target_release_at is not None
                else None
            ),
            information_cutoff_at=bundle.information_cutoff_at,
            event_release_at=bundle.target_release_at,
            warnings=tuple((*bundle.warnings, *bundle.missingness)),
            status=handoff.status.value,
            stage=handoff.status.value,
            readiness=ProviderReadiness(
                mode=SessionMode.FIXTURE_REPLAY,
                ready=True,
                detail="canonical D0 fixture replay; provider and model calls are disabled",
            ),
            handoff=handoff,
            artifact_refs=refs,
            context_refs=context_refs,
            baseline_ref=baseline_ref,
            handoff_ref=handoff_ref,
            created_at=datetime.now(timezone.utc),
        )
        return session

    def _submit_live(
        self,
        question: str,
        idempotency_key: str,
        session_id: str,
        *,
        ticker: str | None = None,
    ) -> ProductSession:
        ticker = ticker or infer_ticker(question)
        readiness = _live_readiness(self.settings, self.evidence_source)
        if not readiness.ready:
            return ProductSession(
                session_id=session_id,
                idempotency_key=idempotency_key,
                question=question,
                mode=SessionMode.LIVE,
                origin=None,
                ticker=ticker,
                status="error",
                stage="provider_readiness",
                readiness=readiness,
                error=ProductSessionErrorInfo(
                    code="live_provider_not_ready",
                    message=readiness.detail,
                    provider=readiness.missing[0] if readiness.missing else None,
                ),
                created_at=datetime.now(timezone.utc),
            )
        try:
            if self.evidence_source is None:
                raise ValueError("research evidence source is not configured")
            evidence_bundle = self.evidence_source.build(
                ticker=ticker, question=question, session_id=session_id,
            )
            record = product_run.execute_research_ask(
                question,
                settings=self.settings.model_copy(update={"data_mode": "live"}),
                output_dir=self.live_artifact_root,
                session_id=session_id,
                evidence_bundle=evidence_bundle,
                ticker=ticker,
            )
        except Exception as error:  # normalize provider/runner failures at the UI boundary
            return ProductSession(
                session_id=session_id,
                idempotency_key=idempotency_key,
                question=question,
                mode=SessionMode.LIVE,
                origin=None,
                ticker=ticker,
                status="error",
                stage="live_run",
                readiness=readiness,
                error=_safe_error_info(error),
                created_at=datetime.now(timezone.utc),
            )
        result = record.result
        locked = result.locked_replay if isinstance(result.locked_replay, dict) else {}
        context_refs = tuple(
            _verified_reference(
                Path(str(card["path"])),
                kind="context_card",
                expected_sha256=str(card.get("sha256")) if card.get("sha256") else None,
            )
            for card in result.cards
            if isinstance(card, dict) and card.get("path")
        )
        refs: list[ArtifactReference] = [
            _verified_reference(
                record.artifacts.json_path,
                kind="research_json",
                expected_sha256=record.artifacts.json_sha256,
            ),
            _verified_reference(
                record.artifacts.markdown_path,
                kind="research_markdown",
                expected_sha256=record.artifacts.markdown_sha256,
            ),
            *context_refs,
        ]
        baseline_ref = None
        if result.locked_replay is not None:
            baseline_ref = _verified_reference(
                record.artifacts.json_path,
                kind="baseline",
                expected_sha256=record.artifacts.json_sha256,
                subresource="locked_replay",
            )
            refs.append(baseline_ref)
        handoff = self._load_handoff(SessionMode.LIVE, session_id)
        handoff_ref = None
        if handoff is not None:
            handoff_ref = _handoff_state_reference(self.live_artifact_root / "external-agent", handoff)
            refs.extend((_handoff_bundle_reference(handoff), handoff_ref))
        return ProductSession(
            session_id=session_id,
            idempotency_key=idempotency_key,
            question=question,
            mode=SessionMode.LIVE,
            origin=_explicit_origin(result),
            ticker=result.ticker,
            event_id=_optional_text(locked.get("event_id")),
            event_date=_optional_event_date(locked.get("event_release_at")),
            information_cutoff_at=_optional_datetime(
                locked.get("information_cutoff_at") or locked.get("locked_at")
            ),
            event_release_at=_optional_datetime(locked.get("event_release_at")),
            warnings=_result_warnings(result),
            status=handoff.status.value if handoff else result.status,
            stage=handoff.status.value if handoff else result.stage,
            readiness=readiness,
            result=result,
            handoff=handoff,
            artifact_refs=tuple(refs),
            context_refs=context_refs,
            baseline_ref=baseline_ref,
            handoff_ref=handoff_ref,
            created_at=result.created_at,
        )

    def _find_existing(
        self,
        *,
        session_id: str,
        mode: SessionMode,
        question: str,
        idempotency_key: str,
        ticker: str | None,
    ) -> ProductSession | None:
        try:
            existing = self.load(session_id)
        except ProductSessionNotFoundError:
            return None
        if (
            existing.mode is mode
            and existing.question == question
            and existing.idempotency_key == idempotency_key
            and (ticker is None or existing.ticker == ticker)
        ):
            return existing
        raise ProductSessionCollisionError(
            f"session {session_id!r} is already bound to a different product request"
        )

    def _find_idempotency_collision(self, key: str) -> ProductSession | None:
        if self.settings.memory_backend == "supabase":
            return None
        if not self.sessions_root.is_dir():
            return None
        for path in sorted(self.sessions_root.glob("*.json")):
            try:
                envelope = _PersistedProductSession.model_validate_json(path.read_bytes())
            except (OSError, ValueError) as error:
                raise ProductSessionIntegrityError(
                    f"invalid persisted product session: {path.name}"
                ) from error
            if envelope.session.idempotency_key == key:
                return envelope.session
        return None

    def _merge_handoff(
        self, session: ProductSession, *, expected_revision: int
    ) -> ProductSession:
        handoff = self._load_handoff(session.mode, session.session_id)
        if handoff is None:
            return session
        if handoff.session_id != session.session_id:
            raise ProductSessionIntegrityError("handoff session binding mismatch")
        persisted_handoff = session.handoff
        if persisted_handoff is not None:
            persisted_binding = (
                persisted_handoff.bundle_id,
                persisted_handoff.bundle_revision,
                persisted_handoff.bundle_sha256,
                persisted_handoff.bundle_file_sha256,
            )
            local_binding = (
                handoff.bundle_id,
                handoff.bundle_revision,
                handoff.bundle_sha256,
                handoff.bundle_file_sha256,
            )
            if local_binding != persisted_binding:
                raise ProductSessionIntegrityError("handoff bundle binding mismatch")
            # The repository is authoritative. A local file may advance an
            # awaiting handoff to validated after explicit import, but it may
            # never roll a validated repository revision backward or replace
            # a canonical state at the same lifecycle stage.
            if persisted_handoff.status is ExternalHandoffStatus.VALIDATED:
                handoff = persisted_handoff
            elif handoff.status is not ExternalHandoffStatus.VALIDATED:
                handoff = persisted_handoff
        handoff_ref = _handoff_state_reference(
            self._handoff_root(session.mode), handoff
        )
        bundle_ref = _handoff_bundle_reference(handoff)
        ordered_handoff_refs = [bundle_ref, handoff_ref]
        if handoff.status is ExternalHandoffStatus.VALIDATED:
            if handoff.report_path:
                ordered_handoff_refs.append(
                    _verified_reference(
                        Path(handoff.report_path),
                        kind="handoff_report",
                        expected_sha256=handoff.report_sha256,
                    )
                )
            if handoff.report_markdown_path:
                ordered_handoff_refs.append(
                    _verified_reference(
                        Path(handoff.report_markdown_path),
                        kind="handoff_report_markdown",
                        expected_sha256=handoff.report_markdown_sha256,
                    )
                )
        replacements = {item.kind: item for item in ordered_handoff_refs}
        refs: list[ArtifactReference] = []
        for item in session.artifact_refs:
            if item.kind in replacements:
                if item.kind not in {ref.kind for ref in refs}:
                    refs.append(replacements[item.kind])
            else:
                refs.append(item)
        for item in ordered_handoff_refs:
            if item.kind not in {ref.kind for ref in refs}:
                refs.append(item)
        updated = session.model_copy(
            update={
                "handoff": handoff,
                "status": handoff.status.value,
                "stage": handoff.status.value,
                "handoff_ref": handoff_ref,
                "artifact_refs": tuple(refs),
            }
        )
        if updated != session:
            self._persist(updated, expected_revision=expected_revision)
        return updated

    def _verify_references(self, session: ProductSession) -> None:
        for reference in session.artifact_refs:
            _verified_reference(
                reference.path,
                kind=reference.kind,
                expected_sha256=reference.sha256,
                content_sha256=reference.content_sha256,
                subresource=reference.subresource,
            )

    def _load_handoff(
        self, mode: SessionMode, session_id: str
    ) -> ExternalHandoffState | None:
        path = self._handoff_root(mode) / session_id / "handoff-state.json"
        if not path.is_file():
            return None
        try:
            return ExternalAgentHandoffStore(self._handoff_root(mode)).load(session_id)
        except (OSError, ValueError) as error:
            raise ProductSessionIntegrityError("invalid persisted external handoff") from error

    def _handoff_root(self, mode: SessionMode) -> Path:
        return (
            self.fixture_handoff_root
            if mode is SessionMode.FIXTURE_REPLAY
            else self.live_artifact_root / "external-agent"
        )

    def _persist(self, session: ProductSession, *, expected_revision: int) -> int:
        session_value = session.model_dump(mode="json", exclude_none=False)
        session_checksum = _sha256_bytes(_canonical_json(session_value))
        run_id = _product_run_id(
            session.session_id,
            owner_id=self.settings.memory_owner_id,
            workspace_id=self.settings.memory_workspace_id,
        )
        existing = self.repository.list_run_records(
            self.settings.memory_workspace_id, run_id, kind=RunRecordKind.TRANSITION
        )
        current_revision = max((item.revision for item in existing), default=0)
        if current_revision != expected_revision:
            raise ProductSessionStaleRevisionError(
                "product session changed before the transition could be committed"
            )
        revision = expected_revision + 1
        session_uuid = uuid5(run_id, "product-session")
        record_fields = {
            "id": uuid5(run_id, f"transition:{revision}"),
            "owner_id": self.settings.memory_owner_id,
            "workspace_id": self.settings.memory_workspace_id,
            "run_id": run_id,
            "session_id": session_uuid,
            "kind": RunRecordKind.TRANSITION,
            "revision": revision,
            "created_at": datetime.now(timezone.utc),
            "status": f"{session.status}:{session.stage}",
            "artifact": None,
            "metadata": {
                "product_session": session_value,
                "product_session_sha256": session_checksum,
            },
        }
        checksum = canonical_run_record_checksum(
            DurableRunRecord.model_construct(**record_fields, checksum="0" * 64)
        )
        record = DurableRunRecord(
            **record_fields,
            checksum=checksum,
        )
        try:
            self.repository.save_run_record(record)
        except ImmutableRevisionError as error:
            raise ProductSessionStaleRevisionError(
                "product session changed before the transition could be committed"
            ) from error
        self._write_session_mirror(session)
        return revision

    def _write_session_mirror(self, session: ProductSession) -> None:
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        path = self.sessions_root / f"{session.session_id}.json"
        payload = _PersistedProductSession(session=session).model_dump_json(
            indent=None, exclude_none=False
        ).encode("utf-8")
        _atomic_write(path, payload)
        _atomic_write(path.with_suffix(".json.sha256"), (_sha256_bytes(payload) + "\n").encode())

    def _reject_reserved_collision(self, mode: SessionMode, session_id: str) -> None:
        if session_id == FIXTURE_SESSION_ID and mode is SessionMode.LIVE:
            raise ProductSessionCollisionError(
                f"{FIXTURE_SESSION_ID!r} is reserved for fixture_replay"
            )


ProductSessionService = ProductSessionFacade
ProductSessionStore = ProductSessionFacade


def _normalize_mode(mode: SessionMode | str) -> SessionMode:
    try:
        return mode if isinstance(mode, SessionMode) else SessionMode(str(mode).strip().casefold())
    except ValueError as error:
        raise ProductSessionInputError("mode must be fixture_replay or live") from error


def _product_run_id(
    session_id: str, *, owner_id: str, workspace_id: str
) -> UUID:
    """Return a stable run ID scoped to one deployment owner and workspace."""

    return uuid5(
        NAMESPACE_URL,
        f"eventlens:product-session:{owner_id}:{workspace_id}:{session_id}",
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _normalize_question(question: str) -> str:
    if not isinstance(question, str) or not question.strip():
        raise ProductSessionInputError("research question must not be blank")
    return question.strip()


def _normalize_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", normalized):
        raise ProductSessionInputError("ticker must be an explicit market symbol")
    return normalized


def _resolve_requested_session(
    mode: SessionMode,
    *,
    idempotency_key: str | None,
    session_id: str | None,
) -> str:
    if session_id is not None:
        _validate_session_id(session_id)
        if mode is SessionMode.FIXTURE_REPLAY and session_id != FIXTURE_SESSION_ID:
            raise ProductSessionCollisionError(
                f"fixture replay session_id is immutable: {FIXTURE_SESSION_ID}"
            )
        return session_id
    if mode is SessionMode.FIXTURE_REPLAY:
        return FIXTURE_SESSION_ID
    if idempotency_key:
        return f"live-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]}"
    return f"live-{uuid4().hex}"


def _resolve_idempotency_key(
    mode: SessionMode, *, idempotency_key: str | None, session_id: str
) -> str:
    if mode is SessionMode.FIXTURE_REPLAY:
        return f"fixture:{session_id}"
    if idempotency_key is not None:
        return idempotency_key
    return f"session:{session_id}"


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        raise ProductSessionInputError("session_id must be a safe stable artifact identifier")
    if session_id in {".", ".."}:
        raise ProductSessionInputError("session_id must be a safe stable artifact identifier")


def _validate_idempotency_key(key: str) -> None:
    if not isinstance(key, str) or not _SAFE_IDEMPOTENCY_KEY.fullmatch(key):
        raise ProductSessionInputError("idempotency_key must be a safe non-empty identifier")


def _live_readiness(
    settings: EventLensSettings, evidence_source: ResearchEvidenceSource | None,
) -> ProviderReadiness:
    required: list[str] = []
    available: list[str] = []
    missing: list[str] = []
    required.append("research_evidence_source")
    if evidence_source is None:
        missing.append("research_evidence_source")
    else:
        available.append("research_evidence_source")
    if not settings.external_agent_enabled:
        required.append("deepseek")
        if settings.deepseek_api_key is not None:
            available.append("deepseek")
        else:
            missing.append("deepseek")
    if missing:
        return ProviderReadiness(
            mode=SessionMode.LIVE,
            ready=False,
            required=tuple(required),
            available=tuple(available),
            missing=tuple(missing),
            detail="Live run is not ready; configure: " + ", ".join(missing),
        )
    return ProviderReadiness(
        mode=SessionMode.LIVE,
        ready=True,
        required=tuple(required),
        available=tuple(available),
        detail="Required live providers are configured",
    )


def _safe_error_info(error: Exception) -> ProductSessionErrorInfo:
    if isinstance(error, ProviderConfigurationError):
        code = "provider_not_ready"
        provider = "provider"
        message = "A required live provider is not configured."
    elif isinstance(error, ProviderError):
        code = "provider_error"
        provider = "provider"
        message = "A live provider request failed safely; inspect the redacted audit trace."
    elif isinstance(error, ValueError):
        code = "invalid_live_run"
        provider = None
        message = "The live research request failed deterministic validation."
    else:
        code = "live_run_failed"
        provider = None
        message = "The live research run failed safely; inspect the redacted audit trace."
    return ProductSessionErrorInfo(code=code, message=message, provider=provider)


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _optional_event_date(value: object) -> date | None:
    timestamp = _optional_datetime(value)
    return timestamp.date() if timestamp is not None else None


def _result_warnings(result: ResearchAskResult) -> tuple[str, ...]:
    warnings: list[str] = []
    reasoning = result.reasoning
    if isinstance(reasoning, dict):
        for item in reasoning.get("errors", ()):
            if isinstance(item, dict):
                code = _optional_text(item.get("code"))
                if code:
                    warnings.append(f"reasoning: {code}")
    locked = result.locked_replay
    if isinstance(locked, dict):
        decision = locked.get("decision")
        if isinstance(decision, dict):
            for item in decision.get("rationale", ()):
                if isinstance(item, str) and "unavailable" in item.casefold():
                    warnings.append(item)
    return tuple(dict.fromkeys(warnings))


def _explicit_origin(result: ResearchAskResult) -> str | None:
    """Return origin only when a result explicitly carries provider origin."""

    values: set[str] = set()
    for card in result.cards:
        if isinstance(card, dict):
            for key in ("origin", "provider_origin"):
                value = card.get(key)
                if isinstance(value, str) and value.strip():
                    values.add(value.strip().casefold())
            # Product card references deliberately stay small. Recover origin
            # only from the checksum-verified canonical card provenance; never
            # infer it merely from the selected UI mode.
            path_value = card.get("path")
            expected = card.get("sha256")
            if isinstance(path_value, str) and isinstance(expected, str):
                path = Path(path_value)
                try:
                    raw = path.read_bytes()
                    if _sha256_bytes(raw) != expected:
                        continue
                    payload = json.loads(raw)
                except (OSError, ValueError, TypeError):
                    continue
                for provenance in payload.get("source_provenance", ()):
                    if isinstance(provenance, dict):
                        origin = provenance.get("origin")
                        if isinstance(origin, str) and origin.strip():
                            values.add(origin.strip().casefold())
    if result.reasoning and isinstance(result.reasoning.get("origin"), str):
        values.add(str(result.reasoning["origin"]).casefold())
    if values == {"live"}:
        return "live"
    if values and values <= {"live", "cache"}:
        return "live_cached" if "cache" in values else "live"
    if values == {"fixture"} or values == {"fixture_replay"}:
        return "fixture_replay"
    return None


def _verified_reference(
    path: Path,
    *,
    kind: str,
    expected_sha256: str | None = None,
    content_sha256: str | None = None,
    subresource: str | None = None,
) -> ArtifactReference:
    actual = _sha256_file(path)
    if expected_sha256 is not None and actual != expected_sha256:
        raise ProductSessionIntegrityError(f"artifact checksum mismatch: {path.name}")
    return ArtifactReference(
        kind=kind,
        path=path,
        sha256=actual,
        verified=True,
        content_sha256=content_sha256,
        subresource=subresource,
    )


def _handoff_state_reference(root: Path, state: ExternalHandoffState) -> ArtifactReference:
    path = root / state.session_id / "handoff-state.json"
    return _verified_reference(path, kind="handoff_state")


def _handoff_bundle_reference(state: ExternalHandoffState) -> ArtifactReference:
    return _verified_reference(
        Path(state.bundle_path), kind="handoff_bundle", expected_sha256=state.bundle_file_sha256
    )


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as error:
        raise ProductSessionIntegrityError(f"artifact is missing or unreadable: {path}") from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


__all__ = [
    "ArtifactReference",
    "ProductSession",
    "ProductSessionCollisionError",
    "ProductSessionError",
    "ProductSessionErrorInfo",
    "ProductSessionFacade",
    "ProductSessionInputError",
    "ProductSessionIntegrityError",
    "ProductSessionNotFoundError",
    "ProductSessionService",
    "ProductSessionStaleRevisionError",
    "ProductSessionStore",
    "ProviderReadiness",
    "SessionMode",
]
