"""Atomic export/pause/import state for the external-agent boundary."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.artifacts.models import canonical_json_bytes
from eventlens.config import AgentRoute
from eventlens.persistence.audit import JSONLAuditStore

from .external import ExternalAgentBundle, ExternalAgentReport
from .external_validation import (
    ExternalReportValidationError,
    parse_external_report_json,
    validate_external_report,
)
from .external_rendering import render_external_report_markdown


_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[tuple[Path, str], threading.Lock] = {}


class ExternalHandoffStatus(StrEnum):
    AWAITING_EXTERNAL_AGENT = "awaiting_external_agent"
    VALIDATED = "validated"


class ExternalHandoffState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    status: ExternalHandoffStatus
    route: AgentRoute
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    bundle_revision: int = Field(ge=1)
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_path: str = Field(min_length=1)
    report_id: str | None = None
    report_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    report_path: str | None = None
    report_markdown_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    report_markdown_path: str | None = None
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def status_matches_artifacts(self) -> "ExternalHandoffState":
        report_fields = (
            self.report_id,
            self.report_sha256,
            self.report_path,
            self.report_markdown_sha256,
            self.report_markdown_path,
        )
        if self.status is ExternalHandoffStatus.AWAITING_EXTERNAL_AGENT:
            if self.route is not AgentRoute.EXTERNAL or any(item is not None for item in report_fields):
                raise ValueError("awaiting external-agent state cannot contain a report")
        elif any(item is None for item in report_fields):
            raise ValueError("validated agent state requires JSON and Markdown report artifacts")
        return self


class ExternalAgentHandoffStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.audit = JSONLAuditStore(root / "audit.jsonl")

    def export(self, bundle: ExternalAgentBundle) -> ExternalHandoffState:
        with _session_lock(self.root, bundle.session_id):
            return self._export_unlocked(bundle)

    def _export_unlocked(self, bundle: ExternalAgentBundle) -> ExternalHandoffState:
        if bundle.route is not AgentRoute.EXTERNAL:
            raise ValueError("only the external route may enter awaiting_external_agent")
        directory = self.root / _safe_segment(bundle.session_id)
        bundle_path = directory / f"bundle-r{bundle.bundle_revision}.json"
        state_path = directory / "handoff-state.json"
        bundle_bytes = bundle.canonical_bytes() + b"\n"
        bundle_file_sha = hashlib.sha256(bundle_bytes).hexdigest()
        if state_path.exists():
            existing = self.load(bundle.session_id)
            if (
                existing.bundle_id == bundle.bundle_id
                and existing.bundle_revision == bundle.bundle_revision
                and existing.bundle_sha256 == bundle.content_sha256
            ):
                return existing
            raise ValueError("session already has a different external-agent bundle")
        _atomic_write(bundle_path, bundle_bytes)
        state = ExternalHandoffState(
            status=ExternalHandoffStatus.AWAITING_EXTERNAL_AGENT,
            route=bundle.route,
            session_id=bundle.session_id,
            event_id=bundle.event_id,
            bundle_id=bundle.bundle_id,
            bundle_revision=bundle.bundle_revision,
            bundle_sha256=bundle.content_sha256,
            bundle_file_sha256=bundle_file_sha,
            bundle_path=str(bundle_path),
            updated_at=datetime.now(timezone.utc),
        )
        _atomic_write(state_path, _state_bytes(state))
        self._audit("external_agent.bundle_exported", state, "export")
        return state

    def load(self, session_id: str) -> ExternalHandoffState:
        path = self.root / _safe_segment(session_id) / "handoff-state.json"
        return ExternalHandoffState.model_validate_json(path.read_bytes())

    def load_bundle(self, state: ExternalHandoffState) -> ExternalAgentBundle:
        path = Path(state.bundle_path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != state.bundle_file_sha256:
            raise ValueError("external-agent bundle file checksum mismatch")
        bundle = ExternalAgentBundle.model_validate_json(raw)
        if bundle.content_sha256 != state.bundle_sha256:
            raise ValueError("external-agent bundle content checksum mismatch")
        return bundle

    def import_report(
        self,
        session_id: str,
        raw_report: str | bytes,
    ) -> tuple[ExternalHandoffState, ExternalAgentReport]:
        with _session_lock(self.root, session_id):
            return self._import_report_unlocked(session_id, raw_report)

    def _import_report_unlocked(
        self,
        session_id: str,
        raw_report: str | bytes,
    ) -> tuple[ExternalHandoffState, ExternalAgentReport]:
        state = self.load(session_id)
        bundle = self.load_bundle(state)
        try:
            report = parse_external_report_json(raw_report)
            validate_external_report(bundle, report)
        except ExternalReportValidationError as error:
            self._audit(
                "external_agent.report_rejected",
                state,
                f"reject:{error.code.value}",
                {"code": error.code.value, "message": error.safe_message},
            )
            raise
        report_bytes = canonical_json_bytes(report.model_dump(mode="json")) + b"\n"
        report_sha = hashlib.sha256(report_bytes).hexdigest()
        if state.status is ExternalHandoffStatus.VALIDATED:
            if state.report_id == report.report_id and state.report_sha256 == report_sha:
                return state, report
            raise ValueError("bundle revision already consumed by a different report")
        report_path = Path(state.bundle_path).parent / f"report-{_safe_segment(report.report_id)}.json"
        markdown_path = report_path.with_suffix(".md")
        markdown_bytes = (render_external_report_markdown(report) + "\n").encode("utf-8")
        markdown_sha = hashlib.sha256(markdown_bytes).hexdigest()
        _atomic_write(report_path, report_bytes)
        _atomic_write(markdown_path, markdown_bytes)
        _atomic_write(markdown_path.with_suffix(".md.sha256"), (markdown_sha + "\n").encode("utf-8"))
        updated = state.model_copy(
            update={
                "status": ExternalHandoffStatus.VALIDATED,
                "report_id": report.report_id,
                "report_sha256": report_sha,
                "report_path": str(report_path),
                "report_markdown_sha256": markdown_sha,
                "report_markdown_path": str(markdown_path),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        _atomic_write(Path(state.bundle_path).parent / "handoff-state.json", _state_bytes(updated))
        self._audit("external_agent.report_validated", updated, "validated")
        return updated, report

    def record_deepseek_result(
        self,
        bundle: ExternalAgentBundle,
        report: ExternalAgentReport,
    ) -> ExternalHandoffState:
        """Persist flag-off DeepSeek output through the same validator contract."""

        with _session_lock(self.root, bundle.session_id):
            return self._record_deepseek_result_unlocked(bundle, report)

    def _record_deepseek_result_unlocked(
        self,
        bundle: ExternalAgentBundle,
        report: ExternalAgentReport,
    ) -> ExternalHandoffState:

        if bundle.route is not AgentRoute.DEEPSEEK:
            raise ValueError("internal reasoning result requires the DeepSeek route")
        validate_external_report(bundle, report)
        directory = self.root / _safe_segment(bundle.session_id)
        state_path = directory / "handoff-state.json"
        bundle_path = directory / f"bundle-r{bundle.bundle_revision}.json"
        bundle_bytes = bundle.canonical_bytes() + b"\n"
        bundle_file_sha = hashlib.sha256(bundle_bytes).hexdigest()
        report_bytes = canonical_json_bytes(report.model_dump(mode="json")) + b"\n"
        report_sha = hashlib.sha256(report_bytes).hexdigest()
        report_path = directory / f"report-{_safe_segment(report.report_id)}.json"
        markdown_path = report_path.with_suffix(".md")
        markdown_bytes = (render_external_report_markdown(report) + "\n").encode("utf-8")
        markdown_sha = hashlib.sha256(markdown_bytes).hexdigest()
        if state_path.exists():
            existing = self.load(bundle.session_id)
            if (
                existing.status is ExternalHandoffStatus.VALIDATED
                and existing.bundle_sha256 == bundle.content_sha256
                and existing.report_id == report.report_id
                and existing.report_sha256 == report_sha
            ):
                return existing
            raise ValueError("session already contains a different agent result")
        _atomic_write(bundle_path, bundle_bytes)
        _atomic_write(report_path, report_bytes)
        _atomic_write(markdown_path, markdown_bytes)
        _atomic_write(markdown_path.with_suffix(".md.sha256"), (markdown_sha + "\n").encode("utf-8"))
        state = ExternalHandoffState(
            status=ExternalHandoffStatus.VALIDATED,
            route=bundle.route,
            session_id=bundle.session_id,
            event_id=bundle.event_id,
            bundle_id=bundle.bundle_id,
            bundle_revision=bundle.bundle_revision,
            bundle_sha256=bundle.content_sha256,
            bundle_file_sha256=bundle_file_sha,
            bundle_path=str(bundle_path),
            report_id=report.report_id,
            report_sha256=report_sha,
            report_path=str(report_path),
            report_markdown_sha256=markdown_sha,
            report_markdown_path=str(markdown_path),
            updated_at=datetime.now(timezone.utc),
        )
        _atomic_write(state_path, _state_bytes(state))
        self._audit("deepseek.report_validated", state, "validated")
        return state

    def _audit(
        self,
        event_type: str,
        state: ExternalHandoffState,
        suffix: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        event = self.audit.new_event(
            event_type=event_type,
            session_id=state.session_id,
            event_scope_id=state.event_id,
            audit_correlation_id=state.bundle_id,
            idempotency_key=f"{state.bundle_id}:r{state.bundle_revision}:{suffix}",
            payload=payload or {"bundle_sha256": state.bundle_sha256},
        )
        self.audit.append(event)


def _safe_segment(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_." else "_" for character in value)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("unsafe artifact path segment")
    return cleaned


def _session_lock(root: Path, session_id: str) -> threading.Lock:
    key = (root.resolve(), session_id)
    with _LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(key, threading.Lock())


def _state_bytes(state: ExternalHandoffState) -> bytes:
    return canonical_json_bytes(state.model_dump(mode="json")) + b"\n"


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
    "ExternalAgentHandoffStore",
    "ExternalHandoffState",
    "ExternalHandoffStatus",
]
