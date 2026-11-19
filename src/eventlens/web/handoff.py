"""UI-neutral external-agent handoff read/write boundary.

The external-agent store is the durable owner of the handoff contract.  This
module is deliberately a small adapter around that store for web clients: it
loads the exact typed state, verifies bundle bytes before returning them, and
turns import failures into safe typed values suitable for a renderer.  It
never executes a route, calls a model, or exposes persisted local paths.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventlens.agents import (
    ExternalAgentHandoffStore,
    ExternalAgentReport,
    ExternalHandoffState,
    ExternalHandoffStatus,
)
from eventlens.agents.external_validation import ExternalReportValidationError
from eventlens.config import AgentRoute, AgentRouteReadiness, EventLensSettings


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SHA256 = r"^[a-f0-9]{64}$"


class HandoffFailureCode(StrEnum):
    """Stable, non-secret failure categories for the web boundary."""

    INVALID_SESSION_ID = "invalid_session_id"
    NOT_FOUND = "not_found"
    BUNDLE_UNAVAILABLE = "bundle_unavailable"
    BUNDLE_CHECKSUM_MISMATCH = "bundle_checksum_mismatch"
    REPORT_INVALID = "report_invalid"
    CONFLICT = "conflict"
    ROUTE_UPLOAD_DISABLED = "route_upload_disabled"
    INTERNAL = "handoff_unavailable"


class HandoffFailure(BaseModel):
    """Safe typed error shown by a UI; no exception/path/provider body leaks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: HandoffFailureCode
    message: str = Field(min_length=1, max_length=500)


class HandoffDownload(BaseModel):
    """Immutable bytes for a browser download, with no filesystem path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    media_type: str = Field(min_length=1)
    content: bytes = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def content_checksum_matches(self) -> "HandoffDownload":
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise ValueError("download checksum does not match content")
        return self


class HandoffChecksumView(BaseModel):
    """The two intentionally distinct checksums in the bundle contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_sha256: str = Field(pattern=_SHA256)
    file_sha256: str = Field(pattern=_SHA256)


class HandoffView(BaseModel):
    """Safe session-scoped handoff read model consumed by Streamlit/tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    route: AgentRoute
    route_readiness: AgentRouteReadiness
    status: str
    bundle_id: str | None = None
    bundle_revision: int | None = None
    checksums: HandoffChecksumView | None = None
    bundle_download: HandoffDownload | None = None
    report_json_download: HandoffDownload | None = None
    report_markdown_download: HandoffDownload | None = None
    upload_enabled: bool = False
    report_available: bool = False
    failure: HandoffFailure | None = None

    @property
    def awaiting_external_agent(self) -> bool:
        return self.status == ExternalHandoffStatus.AWAITING_EXTERNAL_AGENT.value

    @property
    def validated(self) -> bool:
        return self.status == ExternalHandoffStatus.VALIDATED.value


class HandoffImportResult(BaseModel):
    """Result of one explicit report import attempt.

    Invalid input and conflicts are values rather than untyped exceptions so a
    browser upload can render them without exposing implementation details.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    state: ExternalHandoffState | None = None
    report: ExternalAgentReport | None = None
    failure: HandoffFailure | None = None
    idempotent: bool = False
    conflict: bool = False

    @property
    def ok(self) -> bool:
        return self.failure is None and self.state is not None and self.report is not None


class HandoffSessionError(ValueError):
    """Raised only for programmer-facing calls that use an unsafe session ID."""

    def __init__(self, message: str = "session ID is not a safe artifact identifier") -> None:
        super().__init__(message)
        self.code = HandoffFailureCode.INVALID_SESSION_ID
        self.safe_message = message


class SessionHandoffFacade:
    """Session-scoped façade over :class:`ExternalAgentHandoffStore`.

    ``root`` is an application-owned handoff directory.  Callers receive
    typed bytes and metadata only; persisted paths remain an implementation
    detail of the store and are never copied into ``HandoffView``.
    """

    def __init__(self, root: Path, *, settings: EventLensSettings | None = None) -> None:
        self.root = root
        self.settings = settings or EventLensSettings()
        self.store = ExternalAgentHandoffStore(root)

    @staticmethod
    def validate_session_id(session_id: str) -> str:
        if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
            raise HandoffSessionError()
        return session_id

    def load_state(self, session_id: str) -> ExternalHandoffState:
        """Load the exact persisted ``ExternalHandoffState`` after ID checks."""

        self.validate_session_id(session_id)
        state = self.store.load(session_id)
        if state.session_id != session_id:
            raise ValueError("persisted handoff session identity does not match request")
        self._assert_state_paths(state)
        return state

    # Explicit alias used by callers that prefer the store terminology.
    load = load_state

    def view(self, session_id: str) -> HandoffView:
        """Build a secret/path-safe view without invoking an agent route."""

        try:
            self.validate_session_id(session_id)
        except HandoffSessionError as error:
            return self._empty_view(
                "invalid-session",
                HandoffFailure(code=HandoffFailureCode.INVALID_SESSION_ID, message=error.safe_message),
            )

        try:
            state = self.load_state(session_id)
        except FileNotFoundError:
            return self._empty_view(
                session_id,
                HandoffFailure(
                    code=HandoffFailureCode.NOT_FOUND,
                    message="No handoff exists for this session.",
                ),
            )
        except (ValueError, OSError) as error:
            # Preserve a deliberately short, fixed message for filesystem and
            # checksum failures; exception strings can contain local paths.
            code = (
                HandoffFailureCode.BUNDLE_CHECKSUM_MISMATCH
                if "checksum" in str(error).casefold()
                else HandoffFailureCode.BUNDLE_UNAVAILABLE
            )
            return self._empty_view(
                session_id,
                HandoffFailure(code=code, message="The persisted handoff could not be verified."),
            )

        try:
            bundle = self.store.load_bundle(state)
            bundle_bytes = self._verified_file_bytes(
                state.bundle_path,
                expected=state.bundle_file_sha256,
            )
            bundle_download = HandoffDownload(
                filename=f"bundle-r{state.bundle_revision}.json",
                media_type="application/json",
                content=bundle_bytes,
                sha256=state.bundle_file_sha256,
            )
            checksums = HandoffChecksumView(
                content_sha256=state.bundle_sha256,
                file_sha256=state.bundle_file_sha256,
            )
            report_json, report_markdown = self._report_downloads(state)
        except (ValueError, OSError) as error:
            code = (
                HandoffFailureCode.BUNDLE_CHECKSUM_MISMATCH
                if "checksum" in str(error).casefold()
                else HandoffFailureCode.BUNDLE_UNAVAILABLE
            )
            return HandoffView(
                session_id=session_id,
                route=state.route,
                route_readiness=self._readiness(state.route),
                status=state.status.value,
                bundle_id=state.bundle_id,
                bundle_revision=state.bundle_revision,
                checksums=HandoffChecksumView(
                    content_sha256=state.bundle_sha256,
                    file_sha256=state.bundle_file_sha256,
                ),
                upload_enabled=self._upload_enabled(state),
                failure=HandoffFailure(
                    code=code, message="The persisted handoff could not be verified."
                ),
            )

        return HandoffView(
            session_id=session_id,
            route=state.route,
            route_readiness=self._readiness(state.route),
            status=state.status.value,
            bundle_id=state.bundle_id,
            bundle_revision=state.bundle_revision,
            checksums=checksums,
            bundle_download=bundle_download,
            report_json_download=report_json,
            report_markdown_download=report_markdown,
            upload_enabled=self._upload_enabled(state),
            report_available=state.status is ExternalHandoffStatus.VALIDATED,
        )

    # Stable naming for renderers/tests that call this a read model.
    read_model = view

    def bundle_download(self, session_id: str) -> HandoffDownload:
        """Return verified bundle bytes, never a local path."""

        view = self.view(session_id)
        if view.failure is not None:
            raise ValueError(view.failure.message)
        assert view.bundle_download is not None
        return view.bundle_download

    def download_bundle(self, session_id: str) -> HandoffDownload:
        return self.bundle_download(session_id)

    # Names used by simple browser adapters; all remain the same verified
    # byte-returning operation.
    download_bundle_bytes = bundle_download

    def import_report(self, session_id: str, raw_report: str | bytes) -> HandoffImportResult:
        """Explicitly import via the production store validator.

        This method intentionally does not call ``run_configured_agent_route``
        or any model/network adapter.  DeepSeek state is result-only and has no
        upload path through this façade.
        """

        try:
            self.validate_session_id(session_id)
        except HandoffSessionError as error:
            return HandoffImportResult(
                failure=HandoffFailure(
                    code=HandoffFailureCode.INVALID_SESSION_ID, message=error.safe_message
                )
            )
        try:
            before = self.load_state(session_id)
        except FileNotFoundError:
            return HandoffImportResult(
                failure=HandoffFailure(
                    code=HandoffFailureCode.NOT_FOUND,
                    message="No handoff exists for this session.",
                )
            )
        except (ValueError, OSError):
            return HandoffImportResult(
                failure=HandoffFailure(
                    code=HandoffFailureCode.BUNDLE_UNAVAILABLE,
                    message="The persisted handoff could not be verified.",
                )
            )

        if before.route is not AgentRoute.EXTERNAL:
            return HandoffImportResult(
                failure=HandoffFailure(
                    code=HandoffFailureCode.ROUTE_UPLOAD_DISABLED,
                    message="Report upload is disabled for the DeepSeek route; its result is produced internally.",
                )
            )
        try:
            state, report = self.store.import_report(session_id, raw_report)
        except ExternalReportValidationError as error:
            return HandoffImportResult(
                failure=HandoffFailure(code=HandoffFailureCode.REPORT_INVALID, message=error.safe_message)
            )
        except ValueError as error:
            # The store uses ValueError for compare-and-set conflicts.  Do not
            # return its path-bearing text to the browser.
            return HandoffImportResult(
                conflict=True,
                failure=HandoffFailure(
                    code=HandoffFailureCode.CONFLICT,
                    message="This bundle revision already has a different imported report.",
                ),
            )
        return HandoffImportResult(
            state=state,
            report=report,
            idempotent=before.status is ExternalHandoffStatus.VALIDATED,
        )

    resume = import_report

    def _empty_view(self, session_id: str, failure: HandoffFailure) -> HandoffView:
        route = self.settings.agent_route()
        return HandoffView(
            session_id=session_id,
            route=route,
            route_readiness=self._readiness(route),
            status="not_started" if failure.code is HandoffFailureCode.NOT_FOUND else "error",
            upload_enabled=False,
            failure=failure,
        )

    def _readiness(self, route: AgentRoute) -> AgentRouteReadiness:
        configured = self.settings.agent_route_readiness()
        if route is configured.route:
            return configured
        if route is AgentRoute.EXTERNAL:
            return AgentRouteReadiness(
                route=route,
                ready=True,
                detail="external bundle export/import route enabled; no model key required",
            )
        return AgentRouteReadiness(
            route=route,
            ready=False,
            detail="DeepSeek handoff was created under different current route settings",
        )

    @staticmethod
    def _upload_enabled(state: ExternalHandoffState) -> bool:
        return (
            state.route is AgentRoute.EXTERNAL
            and state.status is ExternalHandoffStatus.AWAITING_EXTERNAL_AGENT
        )

    def _assert_state_paths(self, state: ExternalHandoffState) -> None:
        root = self.root.resolve()
        expected_dir = (root / self.validate_session_id(state.session_id)).resolve()
        bundle_path = Path(state.bundle_path).resolve()
        if bundle_path.parent != expected_dir or bundle_path.name != f"bundle-r{state.bundle_revision}.json":
            raise ValueError("persisted bundle path is outside the session handoff directory")
        for path_value in (state.report_path, state.report_markdown_path):
            if path_value is not None and Path(path_value).resolve().parent != expected_dir:
                raise ValueError("persisted report path is outside the session handoff directory")

    def _verified_file_bytes(self, path_value: str, *, expected: str) -> bytes:
        raw = Path(path_value).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("handoff bundle file checksum mismatch")
        return raw

    def _report_downloads(
        self, state: ExternalHandoffState
    ) -> tuple[HandoffDownload | None, HandoffDownload | None]:
        if state.status is not ExternalHandoffStatus.VALIDATED:
            return None, None
        if state.report_path is None or state.report_markdown_path is None:
            raise ValueError("validated handoff is missing report artifacts")
        report_bytes = self._verified_file_bytes(state.report_path, expected=state.report_sha256 or "")
        markdown_bytes = self._verified_file_bytes(
            state.report_markdown_path,
            expected=state.report_markdown_sha256 or "",
        )
        report_id = state.report_id or "report"
        return (
            HandoffDownload(
                filename=f"report-{_safe_filename(report_id)}.json",
                media_type="application/json",
                content=report_bytes,
                sha256=state.report_sha256 or hashlib.sha256(report_bytes).hexdigest(),
            ),
            HandoffDownload(
                filename=f"report-{_safe_filename(report_id)}.md",
                media_type="text/markdown",
                content=markdown_bytes,
                sha256=state.report_markdown_sha256 or hashlib.sha256(markdown_bytes).hexdigest(),
            ),
        )


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned[:100] or "report"


# Descriptive aliases keep the façade discoverable to callers using different
# terms while retaining one implementation and one validation boundary.
ExternalAgentHandoffFacade = SessionHandoffFacade
ExternalHandoffWebService = SessionHandoffFacade


__all__ = [
    "ExternalAgentHandoffFacade",
    "ExternalHandoffWebService",
    "HandoffChecksumView",
    "HandoffDownload",
    "HandoffFailure",
    "HandoffFailureCode",
    "HandoffImportResult",
    "HandoffSessionError",
    "HandoffView",
    "SessionHandoffFacade",
]
