"""Append-only JSONL audit storage for material research-engine activity."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.Lock] = {}
_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "prompt",
        "question",
        "raw_prompt",
        "authorization",
        "credential",
        "credentials",
        "api_key",
        "token",
        "secret",
        "password",
    }
)


class AuditIdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for a different logical event."""


class AuditEvent(BaseModel):
    """One durable audit event serialized as exactly one JSONL line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0", min_length=1)
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_scope_id: str = Field(min_length=1, description="EventLens earnings event ID.")
    audit_correlation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("payload")
    @classmethod
    def redact_sensitive_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Avoid persisting full prompts or credentials in the default audit path."""

        return _redact(value)

    def logical_fingerprint(self) -> str:
        """Hash fields that define an idempotent side effect, excluding clock/ID."""

        payload = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "event_scope_id": self.event_scope_id,
            "audit_correlation_id": self.audit_correlation_id,
            "payload": self.payload,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AuditAppendResult(BaseModel):
    """Outcome of an append attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: AuditEvent
    appended: bool


class JSONLAuditStore:
    """Local append-only JSONL store with in-process idempotency protection.

    The store does not truncate or rewrite prior records.  It serializes
    writers in the process and opens the file with ``O_APPEND``; deployments
    requiring multi-process writers should add an OS-level lock or database
    implementation behind this small interface.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        with _LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(path.resolve(), threading.Lock())

    def new_event(
        self,
        *,
        event_type: str,
        session_id: str,
        event_scope_id: str,
        audit_correlation_id: str,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Create an injectable-clock audit event for a caller to append."""

        return AuditEvent(
            event_id=self._id_factory(),
            event_type=event_type,
            session_id=session_id,
            event_scope_id=event_scope_id,
            audit_correlation_id=audit_correlation_id,
            idempotency_key=idempotency_key,
            occurred_at=self._clock(),
            payload=payload or {},
        )

    def append(self, event: AuditEvent) -> AuditAppendResult:
        """Append an event once, or return the prior equivalent logical event."""

        serialized = event.model_dump_json(by_alias=True) + "\n"
        encoded = serialized.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            existing = self._find_by_idempotency_key(event.idempotency_key)
            if existing is not None:
                if existing.logical_fingerprint() != event.logical_fingerprint():
                    raise AuditIdempotencyConflictError(
                        f"idempotency key {event.idempotency_key!r} belongs to a different audit event"
                    )
                return AuditAppendResult(event=existing, appended=False)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return AuditAppendResult(event=event, appended=True)

    def read_all(self) -> tuple[AuditEvent, ...]:
        """Read and validate every durable event in order."""

        if not self.path.exists():
            return ()
        with self._lock, self.path.open("r", encoding="utf-8") as stream:
            return tuple(
                AuditEvent.model_validate_json(line)
                for line in stream
                if line.strip()
            )

    def _find_by_idempotency_key(self, idempotency_key: str) -> AuditEvent | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                existing = AuditEvent.model_validate_json(line)
                if existing.idempotency_key == idempotency_key:
                    return existing
        return None


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(str(key)) else _redact(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_redact(nested) for nested in value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return (
        normalized in _SENSITIVE_PAYLOAD_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or "credential" in normalized
        or "authorization" in normalized
        or "api_key" in normalized
    )
