"""Typed contracts for the durable EventLens research-memory schema.

These records deliberately keep wall-clock timestamps separate from replay and
knowledge-cutoff timestamps.  They mirror the checked-in Supabase migration but
do not depend on a particular repository adapter.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from eventlens.domain.events import EventTiming


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class MemoryRecord(BaseModel):
    """Strict immutable value returned by either memory adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DataKind(StrEnum):
    REAL = "real"
    SYNTHETIC = "synthetic"


class CompanyMarket(StrEnum):
    US = "US"
    HONG_KONG = "HK"
    OTHER = "OTHER"


class RunMode(StrEnum):
    REPLAY = "replay"
    LIVE = "live"


class SourceType(StrEnum):
    SEC_FILING = "sec_filing"
    ISSUER_RELEASE = "issuer_release"
    ISSUER_TRANSCRIPT = "issuer_transcript"
    MARKET_DATA = "market_data"
    NEWS = "news"
    ANALYST_RESEARCH = "analyst_research"
    FIXTURE = "fixture"
    OTHER = "other"


class ResearchPhase(StrEnum):
    CREATED = "created"
    EVIDENCE_FROZEN = "evidence_frozen"
    AWAITING_HYPOTHESIS = "awaiting_hypothesis"
    HYPOTHESIS_LOCKED = "hypothesis_locked"
    ANALYSIS_COMPLETE = "analysis_complete"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    POST_EVENT = "post_event"
    COMPLETED = "completed"
    FAILED = "failed"


class HypothesisStatus(StrEnum):
    DRAFT = "draft"
    LOCKED = "locked"


class HypothesisAuthorMode(StrEnum):
    ANALYST = "analyst"
    MODEL_DRAFT_ANALYST_APPROVED = "model_draft_analyst_approved"


class VersionStatus(StrEnum):
    DRAFT = "draft"
    FINALIZED = "finalized"


class ContextCardStatus(StrEnum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    STALE = "stale"


class HypothesisDisposition(StrEnum):
    SUPPORTED = "supported"
    WEAKENED = "weakened"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class RecommendationAction(StrEnum):
    BUY = "BUY"
    DONT_BUY = "DONT_BUY"
    TRIM = "TRIM"
    SELL = "SELL"


class HistoricalContextLineageEntry(MemoryRecord):
    """Checksum-addressed prior-card reference persisted with decision artifacts."""

    card_id: NonEmptyStr
    event_id: NonEmptyStr
    ticker: NonEmptyStr
    event_date: date
    knowledge_cutoff_at: UtcDatetime
    content_sha256: Sha256


def historical_context_manifest_sha256(
    entries: tuple[HistoricalContextLineageEntry, ...],
) -> str:
    payload = [entry.model_dump(mode="json") for entry in entries]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_historical_context_lineage(
    entries: tuple[HistoricalContextLineageEntry, ...],
    manifest_sha256: str | None,
    *,
    cutoff_at: datetime,
) -> None:
    if not entries:
        if manifest_sha256 is not None:
            raise ValueError("empty historical context manifest cannot carry a checksum")
        return
    if manifest_sha256 is None:
        raise ValueError("historical context manifest requires a checksum")
    if historical_context_manifest_sha256(entries) != manifest_sha256:
        raise ValueError("historical context manifest checksum does not match entries")
    card_ids = [entry.card_id for entry in entries]
    event_ids = [entry.event_id for entry in entries]
    if len(card_ids) != len(set(card_ids)) or len(event_ids) != len(set(event_ids)):
        raise ValueError("historical context manifest identifiers must be unique")
    if any(entry.knowledge_cutoff_at > cutoff_at for entry in entries):
        raise ValueError("historical context manifest exceeds artifact cutoff")


class CompanyRecord(MemoryRecord):
    id: UUID
    ticker: NonEmptyStr
    exchange: NonEmptyStr
    market: CompanyMarket
    legal_name: NonEmptyStr
    cik: str | None = None
    lei: str | None = None
    primary_currency: CurrencyCode
    active: bool = True
    schema_version: NonEmptyStr
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()


class SourceRecord(MemoryRecord):
    id: UUID
    provider: NonEmptyStr
    external_source_id: str | None = None
    source_type: SourceType
    title: NonEmptyStr
    canonical_url: str | None = None
    published_at: UtcDatetime | None = None
    available_at: UtcDatetime
    retrieved_at: UtcDatetime
    content_sha256: Sha256
    artifact_reference: NonEmptyStr
    size_bytes: int = Field(ge=0)
    media_type: NonEmptyStr
    data_kind: DataKind
    provider_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: NonEmptyStr
    created_at: UtcDatetime

    @model_validator(mode="after")
    def retrieval_does_not_precede_availability(self) -> "SourceRecord":
        if self.retrieved_at < self.available_at:
            raise ValueError("retrieved_at cannot precede available_at")
        expected_storage_reference = (
            "supabase-storage://eventlens-evidence/sha256/" + self.content_sha256
        )
        if not (
            self.artifact_reference == expected_storage_reference
            or self.artifact_reference.startswith("local://")
        ):
            raise ValueError("artifact_reference must be content-addressed storage or local URI")
        return self


class EarningsEventRecord(MemoryRecord):
    id: UUID
    company_id: UUID
    external_event_id: NonEmptyStr
    fiscal_year: int = Field(ge=1900, le=2200)
    fiscal_quarter: int = Field(ge=1, le=4)
    fiscal_period_end: date | None = None
    event_date: date
    timing: EventTiming
    release_at: UtcDatetime
    available_at: UtcDatetime
    timing_confirmed: bool = False
    data_kind: DataKind
    schema_version: NonEmptyStr
    created_at: UtcDatetime
    updated_at: UtcDatetime


class EventMetricRecord(MemoryRecord):
    id: UUID
    event_id: UUID
    source_id: UUID | None = None
    metric_key: NonEmptyStr
    metric_version: int = Field(ge=1)
    value_numeric: Decimal | None = None
    value_text: str | None = None
    value_json: dict[str, Any] | list[Any] | None = None
    unit: str | None = None
    period_label: str | None = None
    observed_at: UtcDatetime | None = None
    available_at: UtcDatetime
    calculation_version: NonEmptyStr
    provenance_sha256: Sha256
    data_kind: DataKind
    schema_version: NonEmptyStr
    created_at: UtcDatetime

    @model_validator(mode="after")
    def exactly_one_value_is_present(self) -> "EventMetricRecord":
        values = (self.value_numeric, self.value_text, self.value_json)
        if sum(value is not None for value in values) != 1:
            raise ValueError("exactly one metric value representation is required")
        return self


class MarketSetupRecord(MemoryRecord):
    id: UUID
    event_id: UUID
    setup_version: int = Field(ge=1)
    as_of_at: UtcDatetime
    information_cutoff_at: UtcDatetime
    available_at: UtcDatetime
    payload: dict[str, Any]
    payload_sha256: Sha256
    calculation_version: NonEmptyStr
    data_kind: DataKind
    schema_version: NonEmptyStr
    created_at: UtcDatetime

    @model_validator(mode="after")
    def setup_is_cutoff_safe(self) -> "MarketSetupRecord":
        if self.as_of_at > self.information_cutoff_at:
            raise ValueError("market setup as_of_at cannot exceed its cutoff")
        if self.available_at > self.information_cutoff_at:
            raise ValueError("market setup cannot be available after its cutoff")
        return self


class MarketReactionRecord(MemoryRecord):
    id: UUID
    event_id: UUID
    reaction_version: int = Field(ge=1)
    reference_session: date
    first_post_event_session: date
    fifth_post_event_session: date | None = None
    available_at: UtcDatetime
    payload: dict[str, Any]
    payload_sha256: Sha256
    calculation_version: NonEmptyStr
    data_kind: DataKind
    schema_version: NonEmptyStr
    created_at: UtcDatetime

    @model_validator(mode="after")
    def reaction_sessions_are_ordered(self) -> "MarketReactionRecord":
        if self.reference_session >= self.first_post_event_session:
            raise ValueError("reference session must precede first post-event session")
        if (
            self.fifth_post_event_session is not None
            and self.fifth_post_event_session < self.first_post_event_session
        ):
            raise ValueError("fifth post-event session cannot precede first post-event session")
        return self


class ResearchSessionRecord(MemoryRecord):
    id: UUID
    session_key: NonEmptyStr
    company_id: UUID
    event_id: UUID
    run_mode: RunMode
    data_kind: DataKind
    phase: ResearchPhase
    information_cutoff_at: UtcDatetime
    replay_as_of_at: UtcDatetime | None = None
    actual_started_at: UtcDatetime
    last_transition_at: UtcDatetime
    compact_state: dict[str, Any] = Field(default_factory=dict)
    state_sha256: Sha256
    workflow_version: NonEmptyStr
    schema_version: NonEmptyStr
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def run_clock_is_unambiguous(self) -> "ResearchSessionRecord":
        if self.run_mode is RunMode.REPLAY:
            if self.replay_as_of_at is None:
                raise ValueError("replay sessions require replay_as_of_at")
            if self.information_cutoff_at > self.replay_as_of_at:
                raise ValueError("information cutoff cannot exceed replay as-of time")
        else:
            if self.replay_as_of_at is not None:
                raise ValueError("live sessions cannot carry replay_as_of_at")
            if self.information_cutoff_at > self.actual_started_at:
                raise ValueError("live cutoff cannot exceed actual start time")
        return self


class HypothesisRecord(MemoryRecord):
    id: UUID
    research_session_id: UUID
    version: int = Field(ge=1)
    status: HypothesisStatus
    author_mode: HypothesisAuthorMode
    run_mode: RunMode
    content: dict[str, Any]
    content_sha256: Sha256
    historical_context_manifest: tuple[HistoricalContextLineageEntry, ...] = ()
    historical_context_manifest_sha256: Sha256 | None = None
    evidence_cutoff_at: UtcDatetime
    replay_as_of_at: UtcDatetime | None = None
    actual_created_at: UtcDatetime
    approved_at: UtcDatetime | None = None
    approved_by: str | None = None
    prompt_version: str | None = None
    schema_version: NonEmptyStr
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def lock_has_explicit_approval(self) -> "HypothesisRecord":
        _validate_historical_context_lineage(
            self.historical_context_manifest,
            self.historical_context_manifest_sha256,
            cutoff_at=self.evidence_cutoff_at,
        )
        if self.run_mode is RunMode.REPLAY:
            if self.replay_as_of_at is None:
                raise ValueError("replay hypothesis requires replay_as_of_at")
            if self.evidence_cutoff_at > self.replay_as_of_at:
                raise ValueError("hypothesis cutoff cannot exceed replay as-of time")
        else:
            if self.replay_as_of_at is not None:
                raise ValueError("live hypothesis cannot carry replay_as_of_at")
            if self.evidence_cutoff_at > self.actual_created_at:
                raise ValueError("live hypothesis cutoff cannot exceed actual creation")
        if self.status is HypothesisStatus.DRAFT:
            if self.approved_at is not None or self.approved_by is not None:
                raise ValueError("draft hypothesis cannot carry approval")
        else:
            if self.approved_at is None or not (self.approved_by or "").strip():
                raise ValueError("locked hypothesis requires approval time and actor")
            if self.approved_at < self.actual_created_at:
                raise ValueError("hypothesis approval cannot precede actual creation")
        return self


class PreEventSnapshotRecord(MemoryRecord):
    id: UUID
    research_session_id: UUID
    event_id: UUID
    version: int = Field(ge=1)
    status: VersionStatus
    run_mode: RunMode
    data_kind: DataKind
    information_cutoff_at: UtcDatetime
    replay_as_of_at: UtcDatetime | None = None
    payload: dict[str, Any]
    content_sha256: Sha256
    historical_context_manifest: tuple[HistoricalContextLineageEntry, ...] = ()
    historical_context_manifest_sha256: Sha256 | None = None
    calculation_version: NonEmptyStr
    workflow_version: NonEmptyStr
    prompt_version: str | None = None
    schema_version: NonEmptyStr
    actual_created_at: UtcDatetime
    finalized_at: UtcDatetime | None = None
    approved_at: UtcDatetime | None = None
    approved_by: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def lifecycle_and_clock_are_explicit(self) -> "PreEventSnapshotRecord":
        _validate_historical_context_lineage(
            self.historical_context_manifest,
            self.historical_context_manifest_sha256,
            cutoff_at=self.information_cutoff_at,
        )
        if self.run_mode is RunMode.REPLAY:
            if self.replay_as_of_at is None:
                raise ValueError("replay snapshot requires replay_as_of_at")
            if self.information_cutoff_at > self.replay_as_of_at:
                raise ValueError("snapshot cutoff cannot exceed replay as-of time")
        elif self.replay_as_of_at is not None:
            raise ValueError("live snapshot cannot carry replay_as_of_at")
        if self.status is VersionStatus.DRAFT:
            if any(
                value is not None
                for value in (self.finalized_at, self.approved_at, self.approved_by)
            ):
                raise ValueError("draft snapshot cannot carry finalization or approval")
        else:
            if (
                self.finalized_at is None
                or self.approved_at is None
                or not (self.approved_by or "").strip()
            ):
                raise ValueError("finalized snapshot requires finalization and approval")
            if self.approved_at < self.finalized_at:
                raise ValueError("snapshot approval cannot precede finalization")
        return self


class HypothesisAssessmentRecord(MemoryRecord):
    id: UUID
    hypothesis_id: UUID
    research_session_id: UUID
    snapshot_id: UUID
    version: int = Field(ge=1)
    status: VersionStatus
    conclusion: HypothesisDisposition
    run_mode: RunMode
    payload: dict[str, Any]
    content_sha256: Sha256
    prompt_version: NonEmptyStr
    model_provider: NonEmptyStr
    model_name: NonEmptyStr
    schema_version: NonEmptyStr
    knowledge_cutoff_at: UtcDatetime
    replay_as_of_at: UtcDatetime | None = None
    actual_created_at: UtcDatetime
    finalized_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def finalization_is_explicit(self) -> "HypothesisAssessmentRecord":
        if self.run_mode is RunMode.REPLAY:
            if self.replay_as_of_at is None:
                raise ValueError("replay assessment requires replay_as_of_at")
            if self.knowledge_cutoff_at > self.replay_as_of_at:
                raise ValueError("assessment cutoff cannot exceed replay as-of time")
        else:
            if self.replay_as_of_at is not None:
                raise ValueError("live assessment cannot carry replay_as_of_at")
            if self.knowledge_cutoff_at > self.actual_created_at:
                raise ValueError("live assessment cutoff cannot exceed actual creation")
        if self.status is VersionStatus.DRAFT and self.finalized_at is not None:
            raise ValueError("draft assessment cannot carry finalized_at")
        if self.status is VersionStatus.FINALIZED:
            if self.finalized_at is None:
                raise ValueError("finalized assessment requires finalized_at")
            if self.finalized_at < self.actual_created_at:
                raise ValueError("assessment finalization cannot precede actual creation")
        return self


class EventContextCardRecord(MemoryRecord):
    id: UUID
    event_id: UUID
    # Joined read-projection fields populated by prior-card repository queries.
    # Optional keeps legacy stored rows backwards-compatible.
    ticker: str | None = None
    event_date: date | None = None
    external_event_id: str | None = None
    research_session_id: UUID
    snapshot_id: UUID
    hypothesis_id: UUID
    hypothesis_assessment_id: UUID
    market_setup_id: UUID
    market_reaction_id: UUID
    version: int = Field(ge=1)
    status: ContextCardStatus
    data_kind: DataKind
    run_mode: RunMode
    knowledge_cutoff_at: UtcDatetime
    replay_as_of_at: UtcDatetime | None = None
    recommendation_action: RecommendationAction | None = None
    recommendation_price_floor: Decimal | None = Field(default=None, ge=0)
    recommendation_price_ceiling: Decimal | None = Field(default=None, ge=0)
    # v4 decision artifacts remain optional so historical v1-v3 payloads stay
    # readable.  A decision-bearing v4 approval must supply this trace.
    decision_trace: dict[str, Any] | None = None
    payload: dict[str, Any]
    content_sha256: Sha256
    historical_context_manifest: tuple[HistoricalContextLineageEntry, ...] = ()
    historical_context_manifest_sha256: Sha256 | None = None
    prompt_version: NonEmptyStr
    model_provider: NonEmptyStr
    model_name: NonEmptyStr
    schema_version: NonEmptyStr
    actual_created_at: UtcDatetime
    reviewed_at: UtcDatetime | None = None
    reviewed_by: str | None = None
    approved_at: UtcDatetime | None = None
    approved_by: str | None = None
    stale_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def lifecycle_clock_and_prices_are_valid(self) -> "EventContextCardRecord":
        _validate_historical_context_lineage(
            self.historical_context_manifest,
            self.historical_context_manifest_sha256,
            cutoff_at=self.knowledge_cutoff_at,
        )
        if any(entry.card_id == str(self.id) for entry in self.historical_context_manifest):
            raise ValueError("context card cannot include itself in historical lineage")
        if self.run_mode is RunMode.REPLAY:
            if self.replay_as_of_at is None:
                raise ValueError("replay context card requires replay_as_of_at")
            if self.knowledge_cutoff_at > self.replay_as_of_at:
                raise ValueError("card knowledge cutoff cannot exceed replay as-of time")
        else:
            if self.replay_as_of_at is not None:
                raise ValueError("live context card cannot carry replay_as_of_at")
            if self.knowledge_cutoff_at > self.actual_created_at:
                raise ValueError("live card knowledge cutoff cannot exceed actual creation")
        if (
            self.recommendation_price_floor is not None
            and self.recommendation_price_ceiling is not None
            and self.recommendation_price_floor > self.recommendation_price_ceiling
        ):
            raise ValueError("recommendation price floor cannot exceed ceiling")
        is_v4 = self.schema_version.startswith("eventlens.context_card.v4")
        if not is_v4 and self.status not in {
            ContextCardStatus.DRAFT,
            ContextCardStatus.APPROVED,
        }:
            raise ValueError("legacy context cards support only draft or approved status")
        if self.status is ContextCardStatus.DRAFT:
            if any(value is not None for value in (self.reviewed_at, self.reviewed_by, self.approved_at, self.approved_by, self.stale_at)):
                raise ValueError("draft context card cannot carry lifecycle timestamps")
        elif self.status is ContextCardStatus.BLOCKED:
            if self.approved_at is not None or self.approved_by is not None or self.stale_at is not None:
                raise ValueError("blocked context card cannot carry approval or staleness")
        elif self.status is ContextCardStatus.REVIEWED:
            if self.reviewed_at is None or not (self.reviewed_by or "").strip():
                raise ValueError("reviewed context card requires review time and actor")
            if self.approved_at is not None or self.approved_by is not None or self.stale_at is not None:
                raise ValueError("reviewed context card cannot carry approval or staleness")
        elif self.status is ContextCardStatus.APPROVED:
            if self.approved_at is None or not (self.approved_by or "").strip():
                raise ValueError("approved context card requires approval time and actor")
            if not is_v4 and self.recommendation_action is None:
                raise ValueError("approved context card requires a recommendation action")
            if is_v4 and self.recommendation_action is not None and not self.decision_trace:
                raise ValueError("approved decision-bearing v4 context card requires decision_trace")
            if self.approved_at < self.actual_created_at:
                raise ValueError("context-card approval cannot precede actual creation")
        else:  # stale
            if self.stale_at is None:
                raise ValueError("stale context card requires stale_at")
            if self.stale_at < self.actual_created_at:
                raise ValueError("context-card staleness cannot precede actual creation")
        return self


class EvidenceLinkRecord(MemoryRecord):
    """Passage-addressable source link used by snapshots, assessments, or cards."""

    source_id: UUID
    passage_id: NonEmptyStr
    relationship: NonEmptyStr
    available_at: UtcDatetime | None = None


class SourcePassageRecord(MemoryRecord):
    id: UUID
    source_id: UUID
    passage_id: NonEmptyStr
    ordinal: int = Field(ge=0)
    text: NonEmptyStr
    text_sha256: Sha256
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    token_estimate: int = Field(ge=0)
    extraction_version: NonEmptyStr
    created_at: UtcDatetime

    @model_validator(mode="after")
    def passage_offsets_are_ordered(self) -> "SourcePassageRecord":
        if self.char_end < self.char_start:
            raise ValueError("passage char_end cannot precede char_start")
        if self.text_sha256 != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("source passage checksum does not match text")
        return self


class RelationSizeRecord(MemoryRecord):
    relation_name: NonEmptyStr
    row_estimate: int = Field(ge=0)
    table_bytes: int = Field(ge=0)
    index_bytes: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class EvidenceStorageSizeRecord(MemoryRecord):
    object_count: int = Field(ge=0)
    content_bytes: int = Field(ge=0)
