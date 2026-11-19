"""Immutable schemas for evaluation examples, outcomes, and scorecards."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventlens.committee.packet import DecisionPacket
from eventlens.graph.models import AnalystHypothesis


class EvaluationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    WARNING = "warning"


class OutcomeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    outcome_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    event_date: date
    collected_at: datetime
    transcript_delta: tuple[dict[str, Any], ...] = ()
    one_session_signed_move_pct: Decimal
    one_session_absolute_move_pct: Decimal = Field(ge=0)
    five_session_signed_move_pct: Decimal
    five_session_absolute_move_pct: Decimal = Field(ge=0)
    source_checksums: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def movement_and_time_are_consistent(self) -> "OutcomeRecord":
        if self.one_session_absolute_move_pct != abs(self.one_session_signed_move_pct):
            raise ValueError("one-session absolute move must equal signed magnitude")
        if self.five_session_absolute_move_pct != abs(self.five_session_signed_move_pct):
            raise ValueError("five-session absolute move must equal signed magnitude")
        if self.collected_at.tzinfo is None or self.collected_at.utcoffset() is None:
            raise ValueError("outcome collection timestamp must be timezone-aware")
        return self


class ApprovedPostmortem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    postmortem_id: str = Field(min_length=1)
    outcome_id: str = Field(min_length=1)
    draft_text: str = Field(min_length=1)
    approved_text: str = Field(min_length=1)
    analyst_edits: tuple[str, ...] = ()
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    error_taxonomy: tuple[str, ...] = ()


class PreEventFeature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_id: str = Field(min_length=1)
    value: Any
    available_at: datetime
    source_id: str = Field(min_length=1)


class EvaluationExample(BaseModel):
    """Immutable joined pre/post record with an explicit temporal barrier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    example_id: str = Field(min_length=1)
    created_at: datetime
    information_cutoff_at: datetime
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    event_date: date
    sector: str | None = None
    industry: str | None = None
    market_regime: str | None = None
    event_type: str = "earnings"
    pre_event_features: tuple[PreEventFeature, ...]
    locked_hypothesis: AnalystHypothesis
    decision_packet: DecisionPacket
    expected_move_estimate_pct: Decimal | None = Field(default=None, ge=0)
    expected_move_type: str = Field(min_length=1)
    outcome: OutcomeRecord
    approved_postmortem: ApprovedPostmortem
    provider_availability: dict[str, bool]
    model_versions: tuple[str, ...]
    prompt_versions: tuple[str, ...]
    quant_parameters: dict[str, Any]
    human_approvals: tuple[str, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def enforce_cutoff_and_identity(self) -> "EvaluationExample":
        if any(item.available_at > self.information_cutoff_at for item in self.pre_event_features):
            raise ValueError("post-event evidence cannot enter pre-event features")
        if self.outcome.event_id != self.event_id or self.outcome.session_id != self.session_id:
            raise ValueError("outcome identity must match evaluation example")
        if self.approved_postmortem.outcome_id != self.outcome.outcome_id:
            raise ValueError("postmortem must approve the retained outcome")
        if self.decision_packet.event_id != self.event_id:
            raise ValueError("decision packet identity must match evaluation example")
        expected = evaluation_content_checksum(self.model_dump(mode="json", exclude={"content_sha256"}))
        if self.content_sha256 != expected:
            raise ValueError("evaluation example checksum does not match immutable content")
        return self


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_name: str = Field(min_length=1)
    grader_version: str = Field(min_length=1)
    example_id: str = Field(min_length=1)
    status: EvaluationStatus
    score: Decimal | None = Field(default=None, ge=0, le=1)
    detail: str = Field(min_length=1)
    deterministic: bool = True
    prompt_version: str | None = None


class ReadinessAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    audited_at: datetime
    persisted_packet_count: int = Field(ge=0)
    unique_event_count: int = Field(ge=0)
    complete_outcome_count: int = Field(ge=0)
    approved_postmortem_count: int = Field(ge=0)
    schema_versions: tuple[str, ...]
    schema_drift_detected: bool
    missingness_by_field: dict[str, int]
    missingness_by_provider: dict[str, int]
    minimum_complete_events: int = Field(ge=1)
    ready_for_training: bool
    permitted_mode: str
    reasons: tuple[str, ...]


class ScorecardRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: str
    key: str
    sample_size: int = Field(ge=0)
    mean_score: Decimal | None
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    tiny_sample_warning: bool


def evaluation_content_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
