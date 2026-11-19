"""Typed four-quarter history, deliberately separate from price attribution.

This module joins only records that already have an explicit fiscal identity.
It does not retrieve observations, calculate returns, or infer why a reaction
occurred.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Iterable, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .accounting import ConsensusEps, EarningsQuality, SurpriseComparison
from .guidance import GuidanceEntry
from .metrics import FiscalPeriod, MetricValue, NormalizedMetric, convert_scale
from .reactions import MarketReaction


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
TimestampPolicyVersion = "eventlens.timestamp_proxy.acceptance_receipt.v1"


class _HistoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True)


class ConsensusType(StrEnum):
    """Expectation provenance; management guidance is never sell-side consensus."""

    SELL_SIDE = "sell_side"
    PRIOR_MANAGEMENT_GUIDANCE = "prior_management_guidance"


class Expectation(_HistoryModel):
    """One pre-result expectation for a named completed fiscal quarter."""

    consensus_type: ConsensusType
    fiscal_period: FiscalPeriod
    metrics: dict[NormalizedMetric, MetricValue | None] = Field(default_factory=dict)
    eps: ConsensusEps | None = None
    prior_guidance: tuple[GuidanceEntry, ...] = ()
    missing_metric_reasons: dict[NormalizedMetric, NonEmptyStr] = Field(default_factory=dict)
    evidence_refs: tuple[NonEmptyStr, ...] = ()
    availability_status: str = "cutoff_native"

    @model_validator(mode="after")
    def source_and_period_are_coherent(self) -> "Expectation":
        for metric, value in self.metrics.items():
            if value is None:
                continue
            if metric != value.metric or value.fiscal_period != self.fiscal_period:
                raise ValueError("expectation metrics must match their key and fiscal period")
        if self.eps is not None and self.eps.estimate.fiscal_period != self.fiscal_period:
            raise ValueError("expectation EPS must target its fiscal period")
        null_metrics = {metric for metric, value in self.metrics.items() if value is None}
        if null_metrics != set(self.missing_metric_reasons):
            raise ValueError("null expectation metrics require, and only permit, missing reasons")
        if self.consensus_type is ConsensusType.SELL_SIDE:
            if self.prior_guidance:
                raise ValueError("sell-side consensus cannot contain management guidance")
            if not self.evidence_refs:
                raise ValueError("sell-side expectation requires evidence refs")
            if self.availability_status not in {"cutoff_native", "unknown", "reconstructed_context_only"}:
                raise ValueError("unknown sell-side availability status")
        else:
            if self.metrics or self.eps is not None:
                raise ValueError("management guidance cannot substitute for sell-side consensus")
            if not self.prior_guidance:
                raise ValueError("prior-management-guidance expectation requires guidance entries")
            if self.evidence_refs:
                raise ValueError("prior guidance carries its own evidence refs")
            for entry in self.prior_guidance:
                if entry.target_fiscal_period != self.fiscal_period:
                    raise ValueError("prior guidance must target the historical quarter")
        return self


class ActualResults(_HistoryModel):
    """Issuer actuals for one quarter; unavailable fields stay null with a reason."""

    fiscal_period: FiscalPeriod
    metrics: dict[NormalizedMetric, MetricValue | None] = Field(default_factory=dict)
    missing_metric_reasons: dict[NormalizedMetric, NonEmptyStr] = Field(default_factory=dict)
    earnings_quality: EarningsQuality | None = None
    evidence_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def actuals_target_one_quarter(self) -> "ActualResults":
        for metric, value in self.metrics.items():
            if value is None:
                continue
            if metric != value.metric or value.fiscal_period != self.fiscal_period:
                raise ValueError("actual metrics must match their key and fiscal period")
        null_metrics = {metric for metric, value in self.metrics.items() if value is None}
        if null_metrics != set(self.missing_metric_reasons):
            raise ValueError("null actual metrics require, and only permit, missing reasons")
        if self.earnings_quality is not None:
            for actual in (self.earnings_quality.actuals.gaap, self.earnings_quality.actuals.adjusted):
                if actual is not None and actual.fiscal_period != self.fiscal_period:
                    raise ValueError("earnings quality actuals must target the historical quarter")
        return self


class MetricSurprise(_HistoryModel):
    """A transparent actual-minus-expectation comparison for a non-EPS metric."""

    metric: NormalizedMetric
    expectation_type: ConsensusType
    expected: MetricValue | None = None
    actual: MetricValue | None = None
    delta: FiniteDecimal | None = None
    reason: NonEmptyStr | None = None
    evidence_refs: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def surprise_is_comparable_or_explained(self) -> "MetricSurprise":
        if self.expected is None or self.actual is None:
            if self.delta is not None or self.reason is None:
                raise ValueError("missing surprise operands require a null delta and reason")
            return self
        if self.expected.metric != self.metric or self.actual.metric != self.metric:
            raise ValueError("surprise operands must match metric")
        compatible = (
            self.expected.fiscal_period == self.actual.fiscal_period
            and self.expected.accounting_basis is self.actual.accounting_basis
            and self.expected.growth_basis is self.actual.growth_basis
            and self.expected.scope is self.actual.scope
        )
        if not compatible:
            if self.delta is not None or self.reason is None:
                raise ValueError("incomparable surprise requires a null delta and reason")
            return self
        try:
            expected_value = convert_scale(self.expected.value, self.expected.unit, self.actual.unit)
        except ValueError:
            expected_value = None
        if expected_value is None or self.actual.value is None:
            if self.delta is not None or self.reason is None:
                raise ValueError("unknown surprise values require a null delta and reason")
        elif self.delta != self.actual.value - expected_value or self.reason is not None:
            raise ValueError("comparable surprise delta must equal actual minus expected")
        return self


class SurpriseLayers(_HistoryModel):
    """Separate sell-side and prior-guidance surprise layers, never conflated."""

    vs_sell_side: tuple[MetricSurprise, ...] = ()
    vs_prior_guidance: tuple[MetricSurprise, ...] = ()
    eps_vs_sell_side: SurpriseComparison | None = None

    @model_validator(mode="after")
    def layers_keep_expectation_provenance(self) -> "SurpriseLayers":
        if any(item.expectation_type is not ConsensusType.SELL_SIDE for item in self.vs_sell_side):
            raise ValueError("sell-side surprise layer requires sell-side expectations")
        if any(item.expectation_type is not ConsensusType.PRIOR_MANAGEMENT_GUIDANCE for item in self.vs_prior_guidance):
            raise ValueError("guidance surprise layer requires prior-management-guidance expectations")
        return self


class EventTimestampEvidence(_HistoryModel):
    """Bounded public-artifact timestamp evidence for a historical event.

    This is not an issuer-release-time assertion.  It records the first
    verified public artifact time available in the staged source receipt.
    """

    timestamp_kind: Literal["first_verified_public_artifact_proxy"] = (
        "first_verified_public_artifact_proxy"
    )
    first_verified_at: AwareDatetime
    timezone: Literal["UTC"] = "UTC"
    source_url: NonEmptyStr
    source_sha256: Sha256
    acceptance_receipt: NonEmptyStr
    evidence_receipt_id: NonEmptyStr
    policy_version: Literal[TimestampPolicyVersion] = TimestampPolicyVersion
    limitation: Literal[
        "SEC acceptance timestamp proxy only; not an issuer earnings-release timestamp."
    ] = "SEC acceptance timestamp proxy only; not an issuer earnings-release timestamp."

    @field_validator("first_verified_at")
    @classmethod
    def timestamp_must_be_utc(cls, value: object) -> object:
        if (
            hasattr(value, "utcoffset")
            and value.utcoffset() is not None
            and value.utcoffset().total_seconds() == 0
        ):
            return value
        raise ValueError("proxy event timestamp must be an exact UTC timestamp")


class HistoricalQuarter(_HistoryModel):
    """The five-stage record for exactly one completed issuer fiscal quarter."""

    fiscal_period: FiscalPeriod
    event_timestamp_evidence: EventTimestampEvidence | None = None
    expectations: tuple[Expectation, ...] = Field(min_length=1)
    actual_results: ActualResults
    surprise_layers: SurpriseLayers
    forward_guidance: tuple[GuidanceEntry, ...] = ()
    reaction: MarketReaction | None = None
    reaction_missing_reason: NonEmptyStr | None = None
    evidence_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def stages_are_joined_by_fiscal_period(self) -> "HistoricalQuarter":
        if self.actual_results.fiscal_period != self.fiscal_period:
            raise ValueError("actual results must match historical fiscal period")
        if any(item.fiscal_period != self.fiscal_period for item in self.expectations):
            raise ValueError("expectations must match historical fiscal period")
        if len({item.consensus_type for item in self.expectations}) != len(self.expectations):
            raise ValueError("only one expectation per consensus type is allowed")
        if any(entry.issued_fiscal_period != self.fiscal_period for entry in self.forward_guidance):
            raise ValueError("forward guidance must be issued with the historical quarter")
        if self.reaction is not None:
            if self.reaction_missing_reason is not None:
                raise ValueError("reaction missing reason requires a missing reaction")
            if self.reaction.fiscal_period != self.fiscal_period.label:
                raise ValueError("reaction must match historical fiscal period")
        elif self.reaction_missing_reason is None:
            raise ValueError("missing reaction requires reaction_missing_reason")
        return self


class HistoricalPattern(_HistoryModel):
    """A qualified historical observation with visible support and exceptions."""

    metric_family: NonEmptyStr
    statement: NonEmptyStr
    supporting_fiscal_periods: tuple[FiscalPeriod, ...] = Field(min_length=2)
    counterexample_fiscal_periods: tuple[FiscalPeriod, ...] = Field(min_length=1)
    confidence: str = "unknown"

    @model_validator(mode="after")
    def supporting_and_counterexample_quarters_are_distinct(self) -> "HistoricalPattern":
        support = set(self.supporting_fiscal_periods)
        if len(support) != len(self.supporting_fiscal_periods):
            raise ValueError("supporting quarters must be unique")
        if support & set(self.counterexample_fiscal_periods):
            raise ValueError("counterexamples cannot also be supporting quarters")
        return self


def assemble_quarter(
    *,
    fiscal_period: FiscalPeriod,
    expectations: Iterable[Expectation],
    actual_results: ActualResults,
    surprise_layers: SurpriseLayers,
    forward_guidance: Iterable[GuidanceEntry] = (),
    reaction: MarketReaction | None = None,
    reaction_missing_reason: str | None = None,
    evidence_refs: Iterable[str],
) -> HistoricalQuarter:
    """Assemble and validate a five-stage quarter without deriving new facts."""
    return HistoricalQuarter(
        fiscal_period=fiscal_period,
        expectations=tuple(expectations),
        actual_results=actual_results,
        surprise_layers=surprise_layers,
        forward_guidance=tuple(forward_guidance),
        reaction=reaction,
        reaction_missing_reason=reaction_missing_reason,
        evidence_refs=tuple(evidence_refs),
    )


_DEFAULT_RECENCY_WEIGHTS = (Decimal("1"), Decimal("0.75"), Decimal("0.50"), Decimal("0.25"))


def historical_weight(
    metric_family: str,
    quarters_back: int,
    *,
    family_weights: dict[str, tuple[Decimal, ...]] | None = None,
) -> Decimal:
    """Return a family-configurable Q-1-first weight for the four-quarter card.

    ``quarters_back=1`` is Q-1 (the latest completed quarter), not a calendar
    label.  Callers must supply an explicit override for any family with a
    different approved recency rule.
    """
    if not metric_family.strip():
        raise ValueError("metric_family must not be blank")
    if quarters_back not in (1, 2, 3, 4):
        raise ValueError("quarters_back must be in 1..4")
    weights = (family_weights or {}).get(metric_family, _DEFAULT_RECENCY_WEIGHTS)
    if len(weights) != 4 or any(weight < 0 for weight in weights):
        raise ValueError("metric-family recency weights must contain four non-negative values")
    if weights[0] < max(weights[1:]):
        raise ValueError("Q-1 must receive the greatest recency weight")
    return weights[quarters_back - 1]


def latest_four_completed(quarters: Iterable[HistoricalQuarter]) -> tuple[HistoricalQuarter, ...]:
    """Select the most recent four issuer fiscal quarters in explicit FY/Q order."""
    ordered = sorted(
        quarters,
        key=lambda quarter: (quarter.fiscal_period.fiscal_year, quarter.fiscal_period.fiscal_quarter or 0),
        reverse=True,
    )
    if len({quarter.fiscal_period for quarter in ordered}) != len(ordered):
        raise ValueError("historical quarters must have unique fiscal periods")
    return tuple(ordered[:4])


__all__ = [
    "ActualResults", "ConsensusType", "Expectation", "HistoricalPattern", "HistoricalQuarter",
    "MetricSurprise", "SurpriseLayers", "assemble_quarter", "historical_weight", "latest_four_completed",
]
