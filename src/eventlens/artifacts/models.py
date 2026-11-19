"""Common, checksum-addressed envelope for durable EventLens artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable JSON bytes for hashing and byte-for-byte replay tests."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class ArtifactSourceRef(BaseModel):
    """Rights/provenance pointer; raw source bodies are not embedded here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactEnvelope(BaseModel):
    """Strict metadata envelope shared by reports, snapshots, and replay outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    created_at: datetime
    source_lineage: tuple[ArtifactSourceRef, ...] = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    calculation_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload: dict[str, Any]

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @classmethod
    def from_payload(
        cls,
        *,
        artifact_type: str,
        artifact_id: str,
        session_id: str,
        event_id: str,
        source_lineage: tuple[ArtifactSourceRef, ...],
        input_sha256: str,
        calculation_version: str,
        payload: dict[str, Any],
        schema_version: str = "1.0",
        prompt_version: str = "not_applicable",
        created_at: datetime | None = None,
    ) -> "ArtifactEnvelope":
        """Build an envelope while deriving the payload checksum exactly once."""

        return cls(
            schema_version=schema_version,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            session_id=session_id,
            event_id=event_id,
            created_at=created_at or datetime.now(timezone.utc),
            source_lineage=source_lineage,
            input_sha256=input_sha256,
            calculation_version=calculation_version,
            prompt_version=prompt_version,
            payload_sha256=sha256_hex(payload),
            payload=payload,
        )

    def verify_payload_checksum(self) -> bool:
        """Check that the persisted payload still matches its recorded digest."""

        return sha256_hex(self.payload) == self.payload_sha256

    def canonical_bytes(self) -> bytes:
        """Canonical envelope bytes, suitable for an immutable artifact file."""

        return canonical_json_bytes(self.model_dump(mode="json"))


__all__ = ["ArtifactEnvelope", "ArtifactSourceRef", "canonical_json_bytes", "sha256_hex"]
