"""Immutable hypothesis locks and append-only annotation records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import sha256_hex


class HypothesisSnapshot(BaseModel):
    """A versioned pre-outcome hypothesis; annotations never rewrite it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: Literal["draft", "locked"]
    content: dict[str, Any]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_cutoff_at: datetime
    locked_at: datetime | None = None
    locked_by: str | None = None

    @field_validator("evidence_cutoff_at", "locked_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("hypothesis timestamps must be timezone-aware")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def lifecycle_is_explicit(self) -> "HypothesisSnapshot":
        if sha256_hex(self.content) != self.content_sha256:
            raise ValueError("hypothesis content checksum does not match content")
        if self.status == "draft" and (self.locked_at is not None or self.locked_by is not None):
            raise ValueError("draft hypothesis cannot carry lock metadata")
        if self.status == "locked" and (self.locked_at is None or not (self.locked_by or "").strip()):
            raise ValueError("locked hypothesis requires locked_at and locked_by")
        if self.locked_at is not None and self.locked_at < self.evidence_cutoff_at:
            raise ValueError("hypothesis cannot be locked before its evidence cutoff")
        return self

    @classmethod
    def draft(
        cls,
        *,
        snapshot_id: str,
        session_id: str,
        event_id: str,
        version: int,
        content: dict[str, Any],
        evidence_cutoff_at: datetime,
    ) -> "HypothesisSnapshot":
        return cls(
            snapshot_id=snapshot_id,
            session_id=session_id,
            event_id=event_id,
            version=version,
            status="draft",
            content=content,
            content_sha256=sha256_hex(content),
            evidence_cutoff_at=evidence_cutoff_at,
        )

    def lock(self, *, locked_at: datetime, locked_by: str) -> "HypothesisSnapshot":
        """Return a new immutable version; the original draft remains unchanged."""

        return self.model_copy(
            update={
                "version": self.version + 1,
                "status": "locked",
                "locked_at": locked_at,
                "locked_by": locked_by,
            }
        )


class HypothesisAnnotation(BaseModel):
    """Separate append-only commentary addressed to one hypothesis snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    annotation_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    annotation_type: str = Field(min_length=1)
    body: str = Field(min_length=1)
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    created_by: str = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("annotation created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def body_checksum_matches(self) -> "HypothesisAnnotation":
        if sha256_hex(self.body) != self.body_sha256:
            raise ValueError("annotation body checksum does not match body")
        return self

    @classmethod
    def create(
        cls,
        *,
        annotation_id: str,
        snapshot: HypothesisSnapshot,
        version: int,
        annotation_type: str,
        body: str,
        created_at: datetime,
        created_by: str,
    ) -> "HypothesisAnnotation":
        return cls(
            annotation_id=annotation_id,
            snapshot_id=snapshot.snapshot_id,
            session_id=snapshot.session_id,
            event_id=snapshot.event_id,
            version=version,
            annotation_type=annotation_type,
            body=body,
            body_sha256=sha256_hex(body),
            created_at=created_at,
            created_by=created_by,
        )


__all__ = ["HypothesisAnnotation", "HypothesisSnapshot"]
