"""Versioned, adapter-neutral records for the resumable research run graph.

Large bodies belong in artifact storage; these rows carry bounded metadata,
checksums, and references only. Contract version 1 intentionally excludes raw
LangGraph state and provider/agent prompts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .memory_models import Sha256


def canonical_run_record_checksum(value: dict[str, object] | "DurableRunRecord") -> str:
    """Hash the versioned semantic record content, excluding its checksum field."""
    if isinstance(value, DurableRunRecord):
        payload = value.model_dump(mode="json", exclude={"checksum"})
    else:
        payload = {key: item for key, item in value.items() if key != "checksum"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class RunRecordKind(StrEnum):
    TRANSITION = "transition"
    ARTIFACT = "artifact"
    COMPANY_CONTEXT = "company_context"
    EVIDENCE_BUNDLE = "evidence_bundle"
    EXTERNAL_AGENT_REPORT = "external_agent_report"
    DECISION_PACKET = "decision_packet"
    QA_RESULT = "qa_result"
    OUTCOME = "outcome"
    POSTMORTEM = "postmortem"


class RunArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=200)
    storage_key: str = Field(min_length=1, max_length=1000)
    sha256: Sha256
    media_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(ge=0)


class DurableRunRecord(BaseModel):
    """One immutable revision in a workspace-scoped run graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "eventlens.run_record.v1"
    id: UUID
    owner_id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    run_id: UUID
    session_id: UUID
    kind: RunRecordKind
    revision: int = Field(ge=1)
    created_at: AwareDatetime
    status: str = Field(min_length=1, max_length=80)
    checksum: Sha256
    artifact: RunArtifactReference | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_metadata_and_artifact(self) -> "DurableRunRecord":
        if len(str(self.metadata).encode("utf-8")) > 32_768:
            raise ValueError("run record metadata exceeds 32768 bytes; store body as an artifact")
        if self.kind is RunRecordKind.ARTIFACT and self.artifact is None:
            raise ValueError("artifact records require an artifact reference")
        if self.checksum != canonical_run_record_checksum(self):
            raise ValueError("run record checksum does not match semantic content")
        return self
