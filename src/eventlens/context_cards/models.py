"""Decision-neutral canonical types for EventLens context cards.

Ticket 1 intentionally defines only the v4 reasoning surface.  It does not
retrieve data, migrate v3 cards, or produce a recommendation.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .evidence import EvidenceReceipt
from .extraction import SemanticClaim
from .metrics import MetricValue, NormalizedMetric
from .accounting import ConsensusEps, EarningsQuality, SurpriseComparison
from .reactions import MarketReaction
from .attribution import validate_attribution
from .temporal import TemporalContext
from .guidance import GuidanceEntry
from .history import HistoricalQuarter
from .conflicts import Conflict
from .decision import (
    DecisionOutput,
    DecisionType,
    EntryThreshold,
    Scenario,
)


SCHEMA_VERSION = "eventlens.context_card.v4"

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Ticker = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9.-]*$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class _ContextCardModel(BaseModel):
    """Strict immutable base for every object emitted into the public schema."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class EventIdentity(_ContextCardModel):
    """Explicit fiscal and wall-clock identity of the event being evaluated."""

    type: Literal["earnings"] = "earnings"
    fiscal_period: NonEmptyStr
    event_datetime: AwareDatetime | None = None
    timezone: NonEmptyStr | None = None
    event_datetime_status: Literal["confirmed", "estimated", "unknown"] = "unknown"

    @model_validator(mode="after")
    def datetime_state_is_coherent(self) -> "EventIdentity":
        if self.event_datetime_status == "unknown" and self.event_datetime is not None:
            raise ValueError("unknown event datetime status cannot carry event_datetime")
        if self.event_datetime_status != "unknown" and self.event_datetime is None:
            raise ValueError("confirmed or estimated event datetime status requires event_datetime")
        if self.event_datetime is not None and self.timezone is None:
            raise ValueError("event datetime requires an explicit timezone label")
        return self


class CardMetadata(_ContextCardModel):
    """Generation identity of this card, separate from the earnings event."""

    version: Literal[4] = 4
    generated_at: AwareDatetime
    as_of: AwareDatetime
    sha256: Sha256 | None = None
    status: Literal["draft", "blocked", "reviewed", "approved", "stale"] = "draft"

    @model_validator(mode="after")
    def as_of_does_not_follow_generation(self) -> "CardMetadata":
        if self.as_of > self.generated_at:
            raise ValueError("card as_of cannot be later than generated_at")
        return self


class CardIdentity(_ContextCardModel):
    ticker: Ticker
    company: NonEmptyStr
    event: EventIdentity
    card: CardMetadata

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class TemporalState(_ContextCardModel):
    """Event-level cutoff and the observations classified against it."""

    historical_event_cutoff: AwareDatetime
    observations: dict[NonEmptyStr, TemporalContext] = Field(default_factory=dict)

    @model_validator(mode="after")
    def observation_cutoffs_match_event_cutoff(self) -> "TemporalState":
        mismatched = tuple(
            observation_id
            for observation_id, context in self.observations.items()
            if context.historical_event_cutoff != self.historical_event_cutoff
        )
        if mismatched:
            raise ValueError(
                "observation historical_event_cutoff must match temporal_state cutoff: "
                + ", ".join(mismatched)
            )
        return self


class _UnknownSafeNumber(_ContextCardModel):
    """A numeric slot that cannot turn an explicit unknown into false precision."""

    value: FiniteDecimal | None = None
    status: Literal[
        "known",
        "computed",
        "not_computed",
        "unknown",
        "insufficient_calibration",
        "removed_due_to_unverifiable_derivation",
    ] = "unknown"

    @model_validator(mode="after")
    def status_matches_value(self) -> "_UnknownSafeNumber":
        has_value = self.value is not None
        if self.status in {"known", "computed"} and not has_value:
            raise ValueError(f"{self.status} numeric value cannot be null")
        if self.status not in {"known", "computed"} and has_value:
            raise ValueError(f"{self.status} numeric value must remain null")
        return self


class DecisionContext(_ContextCardModel):
    """Evaluation frame only; Ticket 1 does not compute or store an action."""

    decision_type: DecisionType = DecisionType.PRE_EARNINGS_EVENT_TRADE
    horizon_start: AwareDatetime | None = None
    horizon_end: AwareDatetime | None = None
    reference_price: _UnknownSafeNumber = Field(default_factory=_UnknownSafeNumber)
    entry_threshold: EntryThreshold = Field(default_factory=EntryThreshold)
    currency: CurrencyCode | None = None
    long_term_view_explicitly_separate: Literal[True] = True
    decision_status: Literal["not_evaluated"] = "not_evaluated"

    @model_validator(mode="after")
    def horizon_is_ordered(self) -> "DecisionContext":
        if (
            self.horizon_start is not None
            and self.horizon_end is not None
            and self.horizon_end <= self.horizon_start
        ):
            raise ValueError("decision horizon end must follow horizon start")
        return self


class CompanyOntology(_ContextCardModel):
    """Compact ontology identity; the full registry remains a package asset."""

    ontology_id: NonEmptyStr
    version: NonEmptyStr
    reference: NonEmptyStr


class Tier1Observation(_ContextCardModel):
    """One required issuer-ontology observation, including an honest absence."""
    ontology_key: NonEmptyStr
    view: Literal["quarterly", "latest_q4_current_state"]
    fiscal_period: NonEmptyStr
    status: Literal["proposed_human_review_pending", "approved"] = "proposed_human_review_pending"
    value: NonEmptyStr | None = None
    unit: NonEmptyStr | None = None
    basis: NonEmptyStr | None = None
    original_value: FiniteDecimal | None = None
    original_unit: NonEmptyStr | None = None
    conversion_provenance: NonEmptyStr | None = None
    missing_reason: NonEmptyStr | None = None
    source_url: NonEmptyStr | None = None
    source_sha256: Sha256 | None = None
    source_text_sha256: Sha256 | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    passage_sha256: Sha256 | None = None
    available_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def observed_or_explicitly_missing(self) -> "Tier1Observation":
        observed = self.value is not None
        if observed:
            required = (self.unit, self.basis, self.source_url, self.source_sha256, self.source_text_sha256, self.char_start, self.char_end, self.passage_sha256, self.available_at)
            if self.missing_reason is not None or any(item is None for item in required):
                raise ValueError("observed Tier-1 slot requires complete passage provenance")
            if self.char_end <= self.char_start:  # type: ignore[operator]
                raise ValueError("Tier-1 passage offsets must be ordered")
            if self.ontology_key == "capex":
                self._validate_capex_conversion()
            elif any(item is not None for item in (self.original_value, self.original_unit, self.conversion_provenance)):
                raise ValueError("non-capex Tier-1 slots must not carry capex conversion provenance")
        elif self.missing_reason is None:
            raise ValueError("missing Tier-1 slot requires a missing_reason")
        elif any(item is not None for item in (self.original_value, self.original_unit, self.conversion_provenance)):
            raise ValueError("missing Tier-1 slots must not carry conversion provenance")
        return self

    def _validate_capex_conversion(self) -> None:
        if self.unit != "USD_billion":
            raise ValueError("capex Tier-1 slots must use canonical USD_billion units")
        if self.original_value is None or self.original_unit != "USD_million":
            raise ValueError("capex Tier-1 slots must retain typed original USD_million value")
        if self.conversion_provenance != "USD_million divided by 1000 to canonical USD_billion":
            raise ValueError("capex Tier-1 slots require exact conversion provenance")
        try:
            canonical = Decimal(self.value)  # type: ignore[arg-type]
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("capex canonical value must be a decimal string") from exc
        if canonical * Decimal("1000") != self.original_value:
            raise ValueError("capex USD_million to USD_billion conversion mismatch")


class CurrentPreEventState(_ContextCardModel):
    """Latest known state with every quantitative fact using ``MetricValue``."""

    latest_reported_fiscal_period: NonEmptyStr | None = None
    latest_actuals: dict[NormalizedMetric, MetricValue] = Field(default_factory=dict)
    current_management_guidance: dict[NormalizedMetric, MetricValue] = Field(default_factory=dict)
    current_management_guidance_qualitative: dict[NonEmptyStr, NonEmptyStr | None] = Field(
        default_factory=dict
    )
    capacity_state: NonEmptyStr | None = None
    ai_state: dict[NonEmptyStr, NonEmptyStr | None] = Field(default_factory=dict)
    earnings_quality: EarningsQuality | None = None

    @model_validator(mode="after")
    def metric_keys_match_values(self) -> "CurrentPreEventState":
        for metric, value in self.latest_actuals.items():
            if value.metric != metric:
                raise ValueError("latest_actuals key must match MetricValue.metric")
        for metric, value in self.current_management_guidance.items():
            if value.metric != metric:
                raise ValueError("current_management_guidance key must match MetricValue.metric")
        return self


class ManagementGuidanceLedgerEntry(GuidanceEntry):
    """Card-field spelling for the complete management guidance ledger type."""


class ConsensusState(_ContextCardModel):
    """Consensus slots without provider selection or reconciliation logic."""

    status: Literal["missing", "partial", "available", "conflicted"] = "missing"
    fiscal_period: NonEmptyStr | None = None
    metrics: dict[NormalizedMetric, MetricValue] = Field(default_factory=dict)
    blockers: tuple[NonEmptyStr, ...] = ()
    eps: ConsensusEps | None = None

    @model_validator(mode="after")
    def metric_keys_match_values(self) -> "ConsensusState":
        for metric, value in self.metrics.items():
            if value.metric != metric:
                raise ValueError("consensus metric key must match MetricValue.metric")
        return self


class _ThesisItem(_ContextCardModel):
    thesis_id: NonEmptyStr
    statement: NonEmptyStr
    evidence_refs: tuple[NonEmptyStr, ...] = ()
    confidence: Literal["low", "medium", "high", "unknown"] = "unknown"


class _UnresolvedQuestion(_ContextCardModel):
    question_id: NonEmptyStr
    question: NonEmptyStr
    importance: Literal["low", "medium", "high"]
    evidence_required: tuple[NonEmptyStr, ...] = ()


class ThesisState(_ContextCardModel):
    supports: tuple[_ThesisItem, ...] = ()
    risks: tuple[_ThesisItem, ...] = ()
    unresolved: tuple[_UnresolvedQuestion, ...] = ()


class ScenarioModel(_ContextCardModel):
    status: Literal["not_computed", "insufficient_calibration", "computed"] = "not_computed"
    bull: Scenario = Field(default_factory=Scenario)
    base: Scenario = Field(default_factory=Scenario)
    bear: Scenario = Field(default_factory=Scenario)


class Decision(DecisionOutput):
    """Canonical card spelling of the typed Ticket 19 decision output."""


class EvidenceRegistry(_ContextCardModel):
    """Typed, compact receipts and their bounded semantic claims.

    Claims intentionally retain only an exact receipt-sized excerpt.  The
    complete source body remains in immutable research memory, addressed by
    the claim's source and passage pointers.
    """

    receipts: tuple[EvidenceReceipt, ...] = ()
    semantic_claims: tuple[SemanticClaim, ...] = ()

    @property
    def receipts_by_id(self) -> dict[str, EvidenceReceipt]:
        return {receipt.id: receipt for receipt in self.receipts}

    @model_validator(mode="after")
    def receipt_graph_is_resolvable(self) -> "EvidenceRegistry":
        receipt_ids = tuple(receipt.id for receipt in self.receipts)
        if len(set(receipt_ids)) != len(receipt_ids):
            raise ValueError("evidence receipt ids must be unique")

        known_ids = set(receipt_ids)
        for receipt in self.receipts:
            missing = set(receipt.input_receipt_ids) - known_ids
            if missing:
                raise ValueError(
                    f"evidence receipt {receipt.id!r} has unresolved input receipts: "
                    + ", ".join(sorted(missing))
                )
        claim_ids = tuple(claim.id for claim in self.semantic_claims)
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("semantic claim ids must be unique")
        for claim in self.semantic_claims:
            receipt = self.receipts_by_id.get(claim.evidence.id)
            if receipt is None:
                raise ValueError(
                    f"semantic claim {claim.id!r} has an unresolved evidence receipt"
                )
            if receipt != claim.evidence:
                raise ValueError(
                    f"semantic claim {claim.id!r} evidence must match the registry receipt"
                )
        self._assert_acyclic()
        return self

    def _assert_acyclic(self) -> None:
        inputs = {receipt.id: receipt.input_receipt_ids for receipt in self.receipts}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(receipt_id: str) -> None:
            if receipt_id in visiting:
                raise ValueError("evidence receipt dependencies cannot contain cycles")
            if receipt_id in visited:
                return
            visiting.add(receipt_id)
            for input_id in inputs[receipt_id]:
                visit(input_id)
            visiting.remove(receipt_id)
            visited.add(receipt_id)

        for receipt_id in inputs:
            visit(receipt_id)


class QualityControl(_ContextCardModel):
    """Persisted QC summary; detailed deterministic findings live in ``qc.py``.

    Strings rather than a direct ``QCReport`` reference intentionally avoid a
    models/QC import cycle while keeping the canonical JSON forward compatible.
    """

    publication_status: Literal["draft", "blocked", "reviewed", "approved", "stale"] = "draft"
    blockers: tuple[NonEmptyStr, ...] = ()
    warnings: tuple[NonEmptyStr, ...] = ()
    requires_human_review: bool = True


class ProvenanceManifest(_ContextCardModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    pipeline_commit: NonEmptyStr | None = None
    ontology_version: NonEmptyStr
    retrieval_version: NonEmptyStr | None = None
    normalization_version: NonEmptyStr | None = None
    reaction_version: NonEmptyStr | None = None
    decision_methodology_version: NonEmptyStr | None = None
    normalized_card_sha256: Sha256 | None = None
    evidence_manifest_sha256: Sha256 | None = None
    source_manifest_sha256: Sha256 | None = None
    generated_at: AwareDatetime


class ContextCardV4(_ContextCardModel):
    """Canonical v4 card envelope.

    Collection fields reserved for later tickets deliberately accept only
    identifiers here, preventing Ticket 1 from inventing population semantics.
    """

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    identity: CardIdentity
    temporal_state: TemporalState
    decision_context: DecisionContext
    current_pre_event_state: CurrentPreEventState
    company_ontology: CompanyOntology
    tier_1_observations: tuple[Tier1Observation, ...] = ()
    historical_quarters: tuple[HistoricalQuarter, ...]
    management_guidance_ledger: tuple[ManagementGuidanceLedgerEntry, ...]
    consensus_state: ConsensusState
    market_reaction_history: tuple[MarketReaction, ...]
    thesis_state: ThesisState
    scenario_model: ScenarioModel
    decision: Decision
    evidence_registry: EvidenceRegistry
    conflicts: tuple[Conflict, ...]
    quality_control: QualityControl
    provenance: ProvenanceManifest

    @model_validator(mode="after")
    def generation_timestamps_match(self) -> "ContextCardV4":
        if self.identity.card.generated_at != self.provenance.generated_at:
            raise ValueError("card and provenance generated_at timestamps must match")
        if self.company_ontology.version != self.provenance.ontology_version:
            raise ValueError(
                "company ontology version must match provenance ontology_version"
            )
        slots = {(item.view, item.fiscal_period, item.ontology_key) for item in self.tier_1_observations}
        if len(slots) != len(self.tier_1_observations):
            raise ValueError("Tier-1 observation slots must be unique")
        if self.company_ontology.ontology_id == "demo_kpi_ontology" and self.tier_1_observations:
            kpis = {"cloud_growth_reported", "cloud_growth_constant_currency", "cloud_forward_guidance", "cloud_gross_margin", "capex", "ai_capacity_constraint"}
            expected = {("quarterly", f"FY2026Q{quarter}", kpi) for quarter in range(1, 5) for kpi in kpis}
            expected |= {("latest_q4_current_state", "FY2026Q4", kpi) for kpi in kpis}
            if slots != expected:
                raise ValueError("DEMO Tier-1 surface must contain exactly the 30 required slots")
        receipt_ids = {receipt.id for receipt in self.evidence_registry.receipts}
        for quarter in self.historical_quarters:
            evidence = quarter.event_timestamp_evidence
            if evidence is None:
                continue
            if evidence.first_verified_at > self.identity.card.as_of:
                raise ValueError("proxy event timestamp cannot be later than card as_of")
            if evidence.evidence_receipt_id not in receipt_ids:
                raise ValueError("proxy event timestamp evidence must link to a registry receipt")
        reactions = list(self.market_reaction_history)
        reactions.extend(
            quarter.reaction
            for quarter in self.historical_quarters
            if quarter.reaction is not None
        )
        for reaction in reactions:
            for attribution in reaction.attributions:
                validate_attribution(
                    attribution,
                    self.evidence_registry.semantic_claims,
                    self.evidence_registry.receipts,
                )
        conflict_ids = tuple(conflict.id for conflict in self.conflicts)
        if len(set(conflict_ids)) != len(conflict_ids):
            raise ValueError("conflict ids must be unique")
        claims = {claim.id: claim for claim in self.evidence_registry.semantic_claims}
        receipts = self.evidence_registry.receipts_by_id
        for conflict in self.conflicts:
            for observation in conflict.observations:
                claim = claims.get(observation.claim_id)
                if claim is None:
                    raise ValueError(
                        f"conflict observation {observation.id!r} has an unresolved semantic claim"
                    )
                receipt = receipts.get(observation.evidence_receipt_id)
                if receipt is None:
                    raise ValueError(
                        f"conflict observation {observation.id!r} has an unresolved evidence receipt"
                    )
                if claim.evidence != receipt:
                    raise ValueError(
                        f"conflict observation {observation.id!r} claim and receipt do not match"
                    )
                if claim.source_ref != observation.source_ref:
                    raise ValueError(
                        f"conflict observation {observation.id!r} source_ref does not match its claim"
                    )
                if claim.metric != observation.metric:
                    raise ValueError(
                        f"conflict observation {observation.id!r} metric does not match its claim"
                    )
        return self
