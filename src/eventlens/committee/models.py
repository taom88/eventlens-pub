"""EventLens-owned, immutable committee contracts.

These contracts deliberately model decision support rather than an execution
instruction.  A desk sees one immutable snapshot and never another desk's
conclusion during its first pass.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.domain.events import EventTiming


class CommitteeRole(StrEnum):
    EARNINGS_RESEARCH = "earnings_research"
    GLOBAL_EQUITIES = "global_equities"
    QUANT_STRATEGY = "quant_strategy"
    TECHNICAL_PANEL = "technical_panel"
    RISK_COMMITTEE = "risk_committee"
    INVESTMENT_COMMITTEE = "investment_committee"
    ADJUDICATOR = "adjudicator"


class InformationKind(StrEnum):
    FACT = "fact"
    DETERMINISTIC_CALCULATION = "deterministic_calculation"
    MODEL_INTERPRETATION = "model_interpretation"
    ANALYST_INPUT = "analyst_input"


class EscalationLevel(StrEnum):
    NONE = "none"
    REVIEW_REQUIRED = "review_required"
    VETO = "veto"


class ClaimSupportStatus(StrEnum):
    """Draft claims remain visible, but cannot be promoted into packet facts."""

    SUPPORTED = "supported"
    DRAFT_UNSUPPORTED = "draft_unsupported"


class EvidenceReference(BaseModel):
    """Passage-addressable evidence; the text itself remains in the ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    critical: bool = False
    refresh_trigger: str | None = None


class HistoricalContextManifestEntry(BaseModel):
    """Frozen, checksum-addressed prior-event memory in a desk snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    event_date: date
    knowledge_cutoff_at: datetime
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executive_summary: str | None = None
    key_messages: tuple[str, ...] = ()
    interpretation: str | None = None

    @field_validator("knowledge_cutoff_at")
    @classmethod
    def cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("historical card cutoff must be timezone-aware")
        return value.astimezone(timezone.utc)


class CanonicalMetric(BaseModel):
    """A calculation owned by EventLens, passed through without agent mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str = Field(min_length=1)
    rendered_value: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    basis: str = Field(min_length=1)


class EvidenceSnapshot(BaseModel):
    """The complete, immutable input set shared by all independent desks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    target_event_date: date | None = None
    information_cutoff_at: datetime
    reported_timing: EventTiming
    effective_timing: EventTiming
    provider_reported_release_at: datetime | None = None
    effective_release_at: datetime
    release_timestamp_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: tuple[EvidenceReference, ...] = ()
    canonical_metrics: tuple[CanonicalMetric, ...] = ()
    evidence_gaps: tuple[EvidenceGap, ...] = ()
    # Compact, checksum-addressed historical memory copied into the immutable
    # desk input set; raw card bodies remain outside the workflow state.
    historical_context_cards: tuple[HistoricalContextManifestEntry, ...] = ()

    @field_validator(
        "information_cutoff_at", "provider_reported_release_at", "effective_release_at"
    )
    @classmethod
    def cutoff_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("information_cutoff_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ids_are_unique(self) -> "EvidenceSnapshot":
        for values, name in (
            (self.evidence, "evidence"),
            (self.canonical_metrics, "canonical metric"),
            (self.evidence_gaps, "evidence gap"),
        ):
            ids = [
                (value.source_id, value.passage_id)
                if isinstance(value, EvidenceReference)
                else value.metric_id if isinstance(value, CanonicalMetric) else value.gap_id
                for value in values
            ]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} identifiers must be unique")
        card_ids = [item.card_id for item in self.historical_context_cards]
        event_ids = [item.event_id for item in self.historical_context_cards]
        if len(card_ids) != len(set(card_ids)) or len(event_ids) != len(set(event_ids)):
            raise ValueError("historical context manifest identifiers must be unique")
        if any(item.knowledge_cutoff_at > self.information_cutoff_at for item in self.historical_context_cards):
            raise ValueError("historical context card cutoff exceeds snapshot cutoff")
        if any(item.ticker.upper() != self.ticker.upper() for item in self.historical_context_cards):
            raise ValueError("historical context card ticker does not match snapshot ticker")
        if self.target_event_date is not None and any(
            item.event_date >= self.target_event_date for item in self.historical_context_cards
        ):
            raise ValueError("historical context card event must precede target event")
        return self


class DeskMandate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: CommitteeRole
    mandate: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)


class DeskClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1)
    proposition_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    stance: str = Field(default="unspecified", min_length=1)
    statement: str = Field(min_length=1, max_length=4_000)
    kind: InformationKind
    support_status: ClaimSupportStatus = ClaimSupportStatus.SUPPORTED
    evidence_references: tuple[EvidenceReference, ...] = ()
    metric_ids: tuple[str, ...] = ()
    assumption_ids: tuple[str, ...] = ()
    missing_data_gap_ids: tuple[str, ...] = ()
    timing_sensitive: bool = False

    @model_validator(mode="after")
    def claim_has_support_or_is_explicitly_qualified(self) -> "DeskClaim":
        has_support = bool(self.evidence_references or self.metric_ids)
        if self.support_status is ClaimSupportStatus.SUPPORTED and self.kind in {
            InformationKind.FACT,
            InformationKind.MODEL_INTERPRETATION,
        } and not has_support:
            raise ValueError("facts and model interpretations require evidence or metrics")
        if self.support_status is ClaimSupportStatus.DRAFT_UNSUPPORTED and has_support:
            raise ValueError("unsupported draft claims cannot carry evidence or metric references")
        return self


class DeskReport(BaseModel):
    """A persisted first-pass desk output, tied to exactly one snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    role: CommitteeRole
    snapshot_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    provider: str = Field(default="unknown", min_length=1)
    model: str = Field(min_length=1)
    generated_at: datetime
    input_adjudication_id: str | None = None
    claims: tuple[DeskClaim, ...] = ()
    conclusion: str = Field(min_length=1)
    caveats: tuple[str, ...] = ()
    refresh_triggers: tuple[str, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def report_claim_ids_are_unique(self) -> "DeskReport":
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("desk claim IDs must be unique")
        return self


class Disagreement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1)
    proposition_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    participating_reports: tuple[str, ...] = Field(min_length=2)
    category: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    differing_assumptions: tuple[str, ...] = ()
    timing_sensitivity: str | None = None
    resolving_evidence: tuple[str, ...] = ()


class DisagreementRegister(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    adjudication_id: str = Field(min_length=1)
    consensus_claim_ids: tuple[str, ...] = ()
    consensus_proposition_ids: tuple[str, ...] = ()
    disputed: tuple[Disagreement, ...] = ()
    unsupported_claim_ids: tuple[str, ...] = ()
    minority_report_ids: tuple[str, ...] = ()
    timing_sensitive_proposition_ids: tuple[str, ...] = ()
    missing_data_gap_ids: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()


class RiskAssessment(BaseModel):
    """An explicit veto does not delete the work of other desks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str = Field(min_length=1)
    escalation: EscalationLevel
    reasons: tuple[str, ...] = ()
    missing_critical_evidence: bool
    timing_uncertainty: bool
    stale_data: bool
    excessive_model_agreement: bool
    unstable_estimates: bool
    insufficient_comparable_events: bool
    unavailable_desks: bool
    cutoff_failure: bool
    unconfirmed_release_timestamp: bool

    @model_validator(mode="after")
    def veto_requires_a_visible_reason(self) -> "RiskAssessment":
        flags = (
            self.missing_critical_evidence,
            self.timing_uncertainty,
            self.stale_data,
            self.excessive_model_agreement,
            self.unstable_estimates,
            self.insufficient_comparable_events,
            self.unavailable_desks,
            self.cutoff_failure,
            self.unconfirmed_release_timestamp,
        )
        if self.escalation is EscalationLevel.VETO and not (self.reasons or any(flags)):
            raise ValueError("a risk veto requires a reason or a flagged condition")
        return self
