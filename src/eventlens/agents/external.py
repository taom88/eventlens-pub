"""Versioned, cutoff-safe external-agent bundle and report contracts."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from pydantic import TypeAdapter

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from .models import Citation
from eventlens.config import AgentRoute

EXTERNAL_AGENT_SCHEMA_VERSION = "1.0"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


Route = AgentRoute


class Capability(_Model):
    capability_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class EvidenceRightsStatus(StrEnum):
    RETENTION_PERMITTED = "retention_permitted"
    SUMMARY_PERMITTED = "summary_permitted"
    METADATA_ONLY = "metadata_only"


class EvidenceRetentionStatus(StrEnum):
    BOUNDED_EXCERPT = "bounded_excerpt"
    SUMMARY_ONLY = "summary_only"
    HEADLINE_ONLY = "headline_only"


class CanonicalMetric(_Model):
    metric_id: str = Field(min_length=1)
    value: Decimal | None = None
    missing_reason: str | None = None
    unit: str = Field(min_length=1)
    period: str = Field(min_length=1)
    as_of: datetime
    calculation_version: str = Field(min_length=1)
    scope: Literal["pre_event"] = "pre_event"
    provenance_ids: tuple[str, ...] = ()
    _as_of_utc = field_validator("as_of")(_utc)

    @model_validator(mode="after")
    def value_or_missing(self) -> "CanonicalMetric":
        if (self.value is None) == (self.missing_reason is None):
            raise ValueError("canonical metric requires exactly one of value or missing_reason")
        return self


class ExternalEvidence(_Model):
    source_id: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)
    evidence_kind: str = Field(min_length=1)
    text: str | None = Field(default=None, max_length=2_000)
    typed_summary: str | None = Field(default=None, max_length=2_000)
    available_at: datetime
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rights_status: EvidenceRightsStatus
    retention_status: EvidenceRetentionStatus
    provenance_ref: str = Field(min_length=1)
    _available_at_utc = field_validator("available_at")(_utc)

    @model_validator(mode="after")
    def bounded_content(self) -> "ExternalEvidence":
        if self.text is None and self.typed_summary is None:
            raise ValueError("evidence requires bounded text or typed_summary")
        if self.text is not None and (
            self.rights_status is not EvidenceRightsStatus.RETENTION_PERMITTED
            or self.retention_status is not EvidenceRetentionStatus.BOUNDED_EXCERPT
        ):
            raise ValueError(
                "verbatim evidence text requires retention permission and bounded_excerpt status"
            )
        if self.retention_status is EvidenceRetentionStatus.SUMMARY_ONLY and self.typed_summary is None:
            raise ValueError("summary_only evidence requires typed_summary")
        if self.rights_status is EvidenceRightsStatus.METADATA_ONLY and (
            self.text is not None
            or self.retention_status is not EvidenceRetentionStatus.HEADLINE_ONLY
        ):
            raise ValueError("metadata_only evidence may retain headline_only content only")
        return self


class HistoricalContextRef(_Model):
    card_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    event_date: date
    knowledge_cutoff_at: datetime
    card_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    readable_summary: str = Field(min_length=1, max_length=4_000)
    key_messages: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    _knowledge_cutoff_utc = field_validator("knowledge_cutoff_at")(_utc)


class ExternalClaim(_Model):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4_000)
    citations: tuple[Citation, ...] = Field(min_length=1)
    kind: Literal["fact", "interpretation"] = "interpretation"
    confidence: Decimal = Field(ge=0, le=1)


class ExternalScenario(_Model):
    scenario_id: Literal["bull", "base", "bear"]
    thesis: str = Field(min_length=1, max_length=4_000)
    probability: Decimal = Field(ge=0, le=1)
    assumptions: tuple[str, ...] = ()
    canonical_metric_ids: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()


class ValuationZone(_Model):
    zone_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    basis: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    lower: Decimal = Field(ge=0)
    upper: Decimal = Field(ge=0)
    assumption_refs: tuple[str, ...] = ()
    calculation_refs: tuple[str, ...] = ()
    metric_refs: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str) -> str:
        value = value.upper()
        if not value.isalpha():
            raise ValueError("currency must be alphabetic")
        return value

    @model_validator(mode="after")
    def valid_zone(self) -> "ValuationZone":
        if self.lower > self.upper:
            raise ValueError("valuation zone lower must not exceed upper")
        if not (self.assumption_refs or self.calculation_refs or self.metric_refs):
            raise ValueError("precise valuation zone requires references")
        return self


class RecommendationAction(StrEnum):
    BUY = "BUY"
    DONT_BUY = "DONT_BUY"
    HOLD = "HOLD"
    TRIM = "TRIM"
    SELL = "SELL"


class ReactionForecast(_Model):
    signed_move_pct: Decimal
    absolute_move_pct: Decimal = Field(ge=0)
    horizon: str = Field(min_length=1)
    uncertainty: str = Field(min_length=1)
    tradeability: str = Field(min_length=1)

    @model_validator(mode="after")
    def absolute_matches_signed(self) -> "ReactionForecast":
        if self.absolute_move_pct != abs(self.signed_move_pct):
            raise ValueError("absolute_move_pct must equal abs(signed_move_pct)")
        return self


class ExternalRecommendation(_Model):
    action: RecommendationAction
    conviction: Decimal = Field(ge=0, le=1)
    thesis: str = Field(min_length=1, max_length=4_000)
    counter_thesis: str = Field(min_length=1, max_length=4_000)
    invalidations: tuple[str, ...] = ()
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    conditions_to_change: tuple[str, ...] = ()


class Abstention(_Model):
    reason: str = Field(min_length=1, max_length=1_000)
    missing_evidence_ids: tuple[str, ...] = ()


class UsageReceipt(_Model):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    _timestamps_utc = field_validator("started_at", "completed_at")(_utc)

    @model_validator(mode="after")
    def ordered(self) -> "UsageReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class ExternalAgentBundle(_Model):
    schema_version: Literal[EXTERNAL_AGENT_SCHEMA_VERSION] = EXTERNAL_AGENT_SCHEMA_VERSION
    bundle_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    route: Route
    bundle_revision: int = Field(ge=1)
    built_at: datetime
    information_cutoff_at: datetime
    target_release_at: datetime | None = None
    target_release_status: str = Field(min_length=1)
    research_question: str = Field(min_length=1, max_length=4_000)
    instructions: tuple[str, ...] = ()
    canonical_metrics: tuple[CanonicalMetric, ...] = ()
    evidence: tuple[ExternalEvidence, ...] = ()
    historical_context: tuple[HistoricalContextRef, ...] = ()
    missingness: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unavailable_stages: tuple[str, ...] = ()
    allowed_skills: tuple[Capability, ...] = ()
    allowed_tools: tuple[Capability, ...] = ()
    forbidden_behaviors: tuple[str, ...] = ()
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    _timestamps_utc = field_validator("built_at", "information_cutoff_at", "target_release_at")(_utc)

    @field_validator("ticker")
    @classmethod
    def ticker_upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def integrity(self, info: ValidationInfo) -> "ExternalAgentBundle":
        metric_ids = [m.metric_id for m in self.canonical_metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("canonical metric IDs must be unique")
        evidence_ids = [(e.source_id, e.passage_id) for e in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        if any(e.available_at > self.information_cutoff_at for e in self.evidence):
            raise ValueError("evidence cannot be available after information cutoff")
        if any(m.as_of > self.information_cutoff_at for m in self.canonical_metrics):
            raise ValueError("canonical metric cannot be as of after information cutoff")
        if any(h.knowledge_cutoff_at > self.information_cutoff_at for h in self.historical_context):
            raise ValueError("historical context cannot be after information cutoff")
        card_ids = [h.card_id for h in self.historical_context]
        if len(card_ids) != len(set(card_ids)):
            raise ValueError("historical card IDs must be unique")
        for capabilities, label in ((self.allowed_skills, "skill"), (self.allowed_tools, "tool")):
            ids = [item.capability_id for item in capabilities]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} IDs must be unique")
        deriving_checksum = bool(info.context and info.context.get("derive_checksum"))
        if not deriving_checksum and self.content_sha256 != self.content_checksum():
            raise ValueError("bundle content checksum does not match content")
        return self

    def content_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"content_sha256"})

    def content_checksum(self) -> str:
        return sha256_hex(self.content_payload())

    @classmethod
    def create(cls, **values: object) -> "ExternalAgentBundle":
        normalized = dict(values)
        provisional = cls.model_validate(
            {**normalized, "content_sha256": "0" * 64},
            context={"derive_checksum": True},
        )
        normalized_payload = provisional.content_payload()
        return cls.model_validate({**normalized_payload, "content_sha256": sha256_hex(normalized_payload)})

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class ExternalAgentReport(_Model):
    schema_version: Literal[EXTERNAL_AGENT_SCHEMA_VERSION] = EXTERNAL_AGENT_SCHEMA_VERSION
    report_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    bundle_revision: int = Field(ge=1)
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    generated_at: datetime
    claims: tuple[ExternalClaim, ...] = ()
    scenarios: tuple[ExternalScenario, ...] = Field(min_length=3, max_length=3)
    reaction_forecast: ReactionForecast | None = None
    recommendation: ExternalRecommendation | None = None
    valuation_zones: tuple[ValuationZone, ...] = ()
    abstention: Abstention | None = None
    warnings: tuple[str, ...] = ()
    usage: UsageReceipt
    _generated_at_utc = field_validator("generated_at")(_utc)

    @model_validator(mode="after")
    def integrity(self) -> "ExternalAgentReport":
        ids = [c.claim_id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim IDs must be unique")
        scenarios = [s.scenario_id for s in self.scenarios]
        if set(scenarios) != {"bull", "base", "bear"} or len(scenarios) != 3:
            raise ValueError("report requires exactly one bull, base, and bear scenario")
        if sum((s.probability for s in self.scenarios), Decimal("0")) != Decimal("1"):
            raise ValueError("scenario probabilities must sum to exactly 1")
        zones = [z.zone_id for z in self.valuation_zones]
        if len(zones) != len(set(zones)):
            raise ValueError("valuation zone IDs must be unique")
        if self.recommendation is None and self.abstention is None:
            raise ValueError("report requires a recommendation or an abstention")
        return self


def external_agent_json_schema() -> dict[str, object]:
    return TypeAdapter(ExternalAgentBundle | ExternalAgentReport).json_schema()


def external_agent_report_json_schema() -> dict[str, object]:
    """Return the report-only schema supplied to reasoning adapters."""

    return ExternalAgentReport.model_json_schema()


__all__ = [
    "EXTERNAL_AGENT_SCHEMA_VERSION", "Route", "Capability", "CanonicalMetric", "ExternalEvidence",
    "EvidenceRightsStatus", "EvidenceRetentionStatus",
    "HistoricalContextRef", "ExternalAgentBundle", "ExternalAgentReport", "ExternalClaim",
    "ExternalScenario", "ExternalRecommendation", "RecommendationAction", "ReactionForecast",
    "ValuationZone", "Abstention", "UsageReceipt", "external_agent_json_schema",
    "external_agent_report_json_schema",
]
