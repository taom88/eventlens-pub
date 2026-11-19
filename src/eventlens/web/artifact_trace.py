"""Secret-safe read APIs for the MVP-205 artifact and audit viewer.

This module is deliberately independent from Streamlit and from the workflow
service.  It turns files on disk into small, serialisable read models that a
presentation layer can render.  Raw file contents never leave this boundary:
JSON values and audit payloads are recursively redacted and Markdown is
scrubbed before it is returned.

The reader is intentionally tolerant of incomplete runs.  A missing or
corrupt file is represented as a record with an explicit status and warning;
it is not converted into an empty successful artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from eventlens.artifacts.models import sha256_hex
from eventlens.persistence.audit import AuditEvent


class TraceStatus(StrEnum):
    """Health of a file presented by the read model."""

    AVAILABLE = "available"
    MISSING = "missing"
    CORRUPT = "corrupt"


class ArtifactMediaType(StrEnum):
    JSON = "application/json"
    MARKDOWN = "text/markdown"
    UNKNOWN = "application/octet-stream"


class ArtifactTraceSpec(BaseModel):
    """A file expected by a run, including optional immutable metadata.

    ``expected_sha256`` is preferred when the producer has a manifest.  When
    omitted, the reader also understands adjacent ``.sha256`` sidecars.
    Missing specs are useful for showing a report that a paused run has not
    produced yet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    artifact_id: str | None = None
    artifact_type: str | None = None
    schema_version: str | None = None
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ArtifactTraceRecord(BaseModel):
    """Safe, UI-neutral description of one JSON or Markdown artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    artifact_type: str
    schema_version: str
    path: str
    media_type: ArtifactMediaType
    status: TraceStatus
    checksum: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_checksum: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    content: dict[str, Any] | list[Any] | str | None = None
    warning: str | None = None


class AuditTraceEvent(BaseModel):
    """Safe subset of an audit event suitable for display."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    event_id: str
    event_type: str
    session_id: str
    event_scope_id: str
    audit_correlation_id: str
    idempotency_key: str
    occurred_at: datetime
    payload: dict[str, Any]


class AuditTrace(BaseModel):
    """Audit read result, including explicit missing/corrupt state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    status: TraceStatus
    events: tuple[AuditTraceEvent, ...] = ()
    warnings: tuple[str, ...] = ()


class SessionArtifactTrace(BaseModel):
    """The allowlisted artifact/audit view for one workflow session.

    This is deliberately a descriptor, not a directory listing.  The service
    constructs the two report descriptors and one audit descriptor explicitly
    for the requested session/event; callers cannot supply a root or glob.
    ``path`` values in nested records are root-relative display paths.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    event_id: str
    artifacts: tuple[ArtifactTraceRecord, ...] = ()
    audit: AuditTrace
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedArtifact:
    metadata: Mapping[str, Any]
    content: dict[str, Any] | list[Any] | str


_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|client[_-]?secret|secret|token|authorization|"
    r"password|credential|private[_-]?key|cookie|session[_-]?token)",
    re.IGNORECASE,
)
_PRIVATE_PROMPT_KEY = re.compile(
    r"(?:^|[_-])(prompt|question|raw[_-]?input|raw[_-]?prompt|user[_-]?message|"
    r"system[_-]?message|instruction|private[_-]?input)(?:$|[_-])",
    re.IGNORECASE,
)
_PATH_KEY = re.compile(
    r"(?:^|[_-])(?:bundle|report|artifact|markdown|audit|file|directory|dir|folder|root|local)?"
    r"[_-]?path(?:$|[_-])",
    re.IGNORECASE,
)
_BEARER_TAIL = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*"
)
_TEXT_SECRET = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?key|client[_-]?secret|secret|token|"
    r"authorization|password|credential)\s*[:=]\s*)(Bearer\s+)?[^\s,;]+"
)
_LOCAL_PATH_TOKEN = re.compile(
    r"(?ix)(?<![A-Za-z0-9_.:/-])(?:"
    r"~[\\/][^\s\"'`,;)}\]]+|"
    r"[A-Z]:[\\/][^\s\"'`,;)}\]]+|"
    r"\\\\[^\s\"'`,;)}\]]+|"
    r"/(?!/)[^\s\"'`,;)}\]]+(?:/[^\s\"'`,;)}\]]+)*"
    r")"
)


def redact_trace_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact credentials and private prompt-like fields.

    Keys are matched case-insensitively and with hyphen/underscore tolerance.
    Values are copied, so callers cannot accidentally mutate a persisted
    payload while rendering it.
    """

    if key is not None and (
        _SECRET_KEY.search(key)
        or _PRIVATE_PROMPT_KEY.search(key)
        or _PATH_KEY.search(key)
    ):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(name): redact_trace_value(nested, key=str(name))
            for name, nested in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_trace_value(item) for item in value]
    if isinstance(value, str):
        return _redact_trace_text(value)
    return value


def _redact_trace_text(value: str) -> str:
    """Redact credentials and local paths embedded in a free-form string."""

    def replace_secret(match: re.Match[str]) -> str:
        # Keep the field name and Bearer scheme useful while removing the
        # credential itself.
        scheme = "Bearer " if match.group(2) else ""
        return f"{match.group(1)}{scheme}[REDACTED]"

    # Handle assignments first, including ``Authorization: Bearer <tail>``
    # as one match. The standalone Bearer pass covers free-form strings.
    text = _TEXT_SECRET.sub(replace_secret, value)
    text = _BEARER_TAIL.sub("Bearer [REDACTED]", text)
    return _LOCAL_PATH_TOKEN.sub("[REDACTED]", text)


def redact_trace_markdown(value: str) -> str:
    """Return Markdown with credential assignments and prompt lines removed."""

    lines: list[str] = []
    for line in value.splitlines():
        if re.search(
            r"(?i)\b(?:private\s+)?(?:prompt|question|user\s+message|system\s+message|"
            r"raw\s+input|instruction)\s*[:=]",
            line,
        ):
            lines.append("[REDACTED]")
        else:
            lines.append(_redact_trace_text(line))
    return "\n".join(lines)


class ArtifactTraceReader:
    """Read artifact files without exposing untrusted or private contents."""

    def __init__(self, root: Path | None = None):
        # Keep both the lexical root and its resolved counterpart.  Resolving
        # only the input path would allow a symlink inside the root to point to
        # another readable file (even when that file resolves back inside the
        # configured tree).  The viewer rejects symlinks explicitly.
        self._lexical_root = (
            Path(root).expanduser().absolute() if root is not None else None
        )
        self.root = self._lexical_root.resolve() if self._lexical_root is not None else None

    def read(self, spec: ArtifactTraceSpec | Path) -> ArtifactTraceRecord:
        """Read one file and return an explicit available/missing/corrupt record."""

        if isinstance(spec, Path):
            spec = ArtifactTraceSpec(path=spec)
        path = Path(spec.path)
        candidate = path if path.is_absolute() else (
            self._lexical_root / path if self._lexical_root is not None else path
        )
        candidate = candidate.absolute()
        resolved, path_error = self._safe_path(candidate)
        if path_error is not None:
            return self._record(
                spec,
                candidate,
                status=TraceStatus.CORRUPT,
                warning=path_error,
            )
        assert resolved is not None
        expected = spec.expected_sha256 or _read_sidecar_checksum(
            resolved, root=self._lexical_root
        )
        metadata = {}
        if not resolved.exists():
            return self._record(
                spec,
                resolved,
                status=TraceStatus.MISSING,
                expected=expected,
                warning="artifact file is missing",
            )
        if not resolved.is_file():
            return self._record(
                spec,
                resolved,
                status=TraceStatus.CORRUPT,
                expected=expected,
                warning="artifact path is not a regular file",
            )
        try:
            raw = resolved.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError) as error:
            return self._record(
                spec,
                resolved,
                status=TraceStatus.CORRUPT,
                expected=expected,
                warning=f"artifact could not be read ({type(error).__name__})",
            )
        if expected is not None and checksum != expected:
            return self._record(
                spec,
                resolved,
                status=TraceStatus.CORRUPT,
                expected=expected,
                checksum=checksum,
                size=len(raw),
                warning="artifact file checksum mismatch",
            )
        try:
            parsed = _parse_artifact(resolved, raw)
            metadata = parsed.metadata
            _validate_embedded_payload_checksum(metadata)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            return self._record(
                spec,
                resolved,
                status=TraceStatus.CORRUPT,
                expected=expected,
                checksum=checksum,
                size=len(raw),
                metadata=metadata,
                warning=f"artifact content is invalid ({type(error).__name__})",
            )
        return self._record(
            spec,
            resolved,
            status=TraceStatus.AVAILABLE,
            expected=expected,
            checksum=checksum,
            size=len(raw),
            metadata=metadata,
            content=parsed.content,
        )

    def read_many(self, specs: Iterable[ArtifactTraceSpec | Path]) -> tuple[ArtifactTraceRecord, ...]:
        """Read specs in canonical path order, retaining missing entries."""

        normalized = [
            item if isinstance(item, ArtifactTraceSpec) else ArtifactTraceSpec(path=item)
            for item in specs
        ]
        return tuple(
            self.read(item)
            for item in sorted(normalized, key=lambda item: self._sort_key(item.path))
        )

    def list(self) -> tuple[ArtifactTraceRecord, ...]:
        """List JSON/Markdown files below ``root`` in stable relative order."""

        if self.root is None or not self.root.exists():
            return ()
        # This legacy convenience method is intentionally non-recursive.  The
        # session service does not use it; it is retained only for callers that
        # already have a dedicated reader root and now cannot accidentally
        # traverse every nested session/run tree.
        paths = (
            path
            for path in self.root.iterdir()
            if path.is_file() and path.suffix.lower() in {".json", ".md", ".markdown"}
        )
        return self.read_many(paths)

    def _safe_path(self, candidate: Path) -> tuple[Path | None, str | None]:
        if self._lexical_root is None:
            # Unscoped convenience reads never expose a path outside the
            # returned basename, but service APIs always provide a root.
            if _contains_symlink(candidate):
                return None, "artifact path uses a symlink"
            try:
                return candidate.resolve(strict=False), None
            except OSError:
                return None, "artifact path could not be resolved"
        if self._lexical_root.is_symlink():
            return None, "configured artifact root must not be a symlink"
        if not _is_within(candidate, self._lexical_root):
            return None, "artifact path is outside the configured read root"
        if _contains_symlink(candidate, root=self._lexical_root):
            return None, "artifact path uses a symlink"
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            return None, "artifact path could not be resolved"
        if not _is_within(resolved, self.root):
            return None, "artifact path resolves outside the configured read root"
        return resolved, None

    def _sort_key(self, path: Path) -> str:
        candidate = Path(path)
        if not candidate.is_absolute() and self._lexical_root is not None:
            candidate = self._lexical_root / candidate
        if self._lexical_root is not None:
            try:
                return candidate.absolute().relative_to(self._lexical_root).as_posix()
            except ValueError:
                return candidate.name
        return candidate.name

    def _record(
        self,
        spec: ArtifactTraceSpec,
        path: Path,
        *,
        status: TraceStatus,
        expected: str | None = None,
        checksum: str | None = None,
        size: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        content: dict[str, Any] | list[Any] | str | None = None,
        warning: str | None = None,
    ) -> ArtifactTraceRecord:
        metadata = metadata or {}
        artifact_id = str(spec.artifact_id or metadata.get("artifact_id") or path.stem)
        artifact_type = str(
            spec.artifact_type
            or metadata.get("artifact_type")
            or _artifact_type(path)
        )
        schema_version = str(
            spec.schema_version
            or metadata.get("schema_version")
            or "unknown"
        )
        safe_content = redact_trace_markdown(content) if isinstance(content, str) else (
            redact_trace_value(content) if content is not None else None
        )
        return ArtifactTraceRecord(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            path=self._display_path(path),
            media_type=_media_type(path),
            status=status,
            checksum=checksum,
            expected_checksum=expected,
            size_bytes=size,
            content=safe_content,
            warning=warning,
        )

    def _display_path(self, path: Path) -> str:
        """Return a stable relative descriptor, never an absolute filesystem path."""

        if self._lexical_root is not None:
            try:
                return path.absolute().relative_to(self._lexical_root).as_posix()
            except ValueError:
                pass
        return path.name or "<artifact>"


class AuditTraceReader:
    """Parse and safely order JSONL audit events for presentation."""

    def __init__(
        self,
        path: Path,
        *,
        root: Path | None = None,
        session_id: str | None = None,
        event_scope_id: str | None = None,
    ):
        self.path = Path(path)
        self._reader = ArtifactTraceReader(root) if root is not None else None
        self.session_id = session_id
        self.event_scope_id = event_scope_id

    def read(self) -> AuditTrace:
        safe_path = self.path
        if self._reader is not None:
            resolved, path_error = self._reader._safe_path(self.path.absolute())
            if path_error is not None:
                return AuditTrace(
                    path=self._reader._display_path(self.path),
                    status=TraceStatus.CORRUPT,
                    warnings=(path_error.replace("artifact", "audit"),),
                )
            assert resolved is not None
            safe_path = resolved
        else:
            safe_path = self.path.absolute()
            if _contains_symlink(safe_path):
                return AuditTrace(
                    path=safe_path.name,
                    status=TraceStatus.CORRUPT,
                    warnings=("audit path uses a symlink",),
                )
        display_path = self._reader._display_path(safe_path) if self._reader else safe_path.name
        if not safe_path.exists():
            return AuditTrace(path=display_path, status=TraceStatus.MISSING, warnings=("audit file is missing",))
        if not safe_path.is_file():
            return AuditTrace(path=display_path, status=TraceStatus.CORRUPT, warnings=("audit path is not a regular file",))
        events: list[AuditTraceEvent] = []
        warnings: list[str] = []
        try:
            lines = safe_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return AuditTrace(path=display_path, status=TraceStatus.CORRUPT, warnings=("audit file could not be read",))
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = AuditEvent.model_validate_json(line)
                if self.session_id is not None and event.session_id != self.session_id:
                    warnings.append("ignored audit event outside the requested session")
                    continue
                if self.event_scope_id is not None and event.event_scope_id != self.event_scope_id:
                    warnings.append("ignored audit event outside the requested event")
                    continue
                events.append(_safe_audit_event(event))
            except (ValueError, TypeError):
                # Do not include validation details: they can echo untrusted input.
                warnings.append(f"invalid audit event at line {line_number}")
        ordered = tuple(
            sorted(
                events,
                key=lambda event: (
                    _utc(event.occurred_at),
                    event.event_id,
                    event.event_type,
                    event.idempotency_key,
                    json.dumps(event.payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )
        )
        status = TraceStatus.CORRUPT if warnings else TraceStatus.AVAILABLE
        return AuditTrace(
            path=display_path,
            status=status,
            events=ordered,
            warnings=tuple(warnings),
        )


def read_artifact(path: Path, *, expected_sha256: str | None = None, root: Path | None = None) -> ArtifactTraceRecord:
    """Convenience function for one artifact read."""

    return ArtifactTraceReader(root).read(ArtifactTraceSpec(path=path, expected_sha256=expected_sha256))


def read_audit_trace(
    path: Path,
    *,
    root: Path | None = None,
    session_id: str | None = None,
    event_scope_id: str | None = None,
) -> AuditTrace:
    """Convenience function for one audit read."""

    return AuditTraceReader(
        path,
        root=root,
        session_id=session_id,
        event_scope_id=event_scope_id,
    ).read()


def _parse_artifact(path: Path, raw: bytes) -> _ParsedArtifact:
    if path.suffix.lower() == ".json":
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, (dict, list)):
            raise ValueError("JSON artifact must contain an object or array")
        metadata = value if isinstance(value, dict) else {}
        return _ParsedArtifact(metadata=metadata, content=value)
    if path.suffix.lower() in {".md", ".markdown"}:
        return _ParsedArtifact(metadata={}, content=raw.decode("utf-8"))
    raise ValueError("unsupported artifact media type")


def _validate_embedded_payload_checksum(metadata: Mapping[str, Any]) -> None:
    expected = metadata.get("payload_sha256")
    payload = metadata.get("payload")
    if expected is None or payload is None:
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("invalid embedded payload checksum")
    if not isinstance(payload, dict) or sha256_hex(payload) != expected:
        raise ValueError("embedded payload checksum mismatch")


def _read_sidecar_checksum(path: Path, *, root: Path | None = None) -> str | None:
    candidates = (path.with_name(path.name + ".sha256"), path.with_suffix(".sha256"))
    for sidecar in candidates:
        try:
            if root is not None:
                if _contains_symlink(sidecar, root=root) or not _is_within(sidecar.absolute(), root.absolute()):
                    continue
            if not sidecar.is_file():
                continue
            value = sidecar.read_text(encoding="ascii").strip().split()[0]
        except (OSError, UnicodeDecodeError, IndexError):
            continue
        if re.fullmatch(r"[a-f0-9]{64}", value):
            return value
    return None


def _safe_audit_event(event: AuditEvent) -> AuditTraceEvent:
    return AuditTraceEvent(
        schema_version=event.schema_version,
        event_id=event.event_id,
        event_type=event.event_type,
        session_id=event.session_id,
        event_scope_id=event.event_scope_id,
        audit_correlation_id=event.audit_correlation_id,
        idempotency_key=event.idempotency_key,
        occurred_at=_utc(event.occurred_at),
        payload=redact_trace_value(event.payload),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _contains_symlink(path: Path, *, root: Path | None = None) -> bool:
    """Whether an existing path component is a symlink.

    Missing leaf components are fine (they produce the explicit ``missing``
    status), while an existing symlink at any component is rejected.
    """

    candidate = path.absolute()
    anchor = root.absolute() if root is not None else candidate.anchor and Path(candidate.anchor)
    try:
        relative = candidate.relative_to(anchor) if anchor is not None else candidate
    except ValueError:
        relative = candidate
    current = anchor if anchor is not None else Path(candidate.anchor)
    for component in relative.parts:
        current = current / component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return False
    return False


def _artifact_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return "json" if suffix == ".json" else "markdown" if suffix in {".md", ".markdown"} else "unknown"


def _media_type(path: Path) -> ArtifactMediaType:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return ArtifactMediaType.JSON
    if suffix in {".md", ".markdown"}:
        return ArtifactMediaType.MARKDOWN
    return ArtifactMediaType.UNKNOWN


__all__ = [
    "ArtifactMediaType",
    "ArtifactTraceReader",
    "ArtifactTraceRecord",
    "ArtifactTraceSpec",
    "AuditTrace",
    "AuditTraceEvent",
    "AuditTraceReader",
    "SessionArtifactTrace",
    "TraceStatus",
    "read_artifact",
    "read_audit_trace",
    "redact_trace_markdown",
    "redact_trace_value",
]
