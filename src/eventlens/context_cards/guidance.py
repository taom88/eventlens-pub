"""Typed, evidence-backed management-guidance ledger.

Company guidance is intentionally a different object from sell-side consensus:
it records what management issued for a named future fiscal period and only
compares it with that period's eventual company result.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from statistics import median
from typing import Annotated, Iterable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .metrics import (
    AccountingBasis,
    FiscalPeriod,
    GrowthBasis,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
    convert_scale,
)


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class GuidanceType(StrEnum):
    RANGE = "range"
    POINT = "point"
    FLOOR = "floor"
    CEILING = "ceiling"
    QUALITATIVE = "qualitative"


class GuidanceStatus(StrEnum):
    BEAT = "beat"
    MET = "met"
    MISS = "miss"
    INCOMPARABLE = "incomparable"
    PENDING = "pending"


class _GuidanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True)


class GuidanceValue(_GuidanceModel):
    """One management-issued target, retaining its original range semantics."""

    type: GuidanceType
    low: FiniteDecimal | None = None
    high: FiniteDecimal | None = None
    point: FiniteDecimal | None = None
    unit: MetricUnit
    accounting_basis: AccountingBasis = AccountingBasis.NOT_APPLICABLE
    growth_basis: GrowthBasis = GrowthBasis.NOT_APPLICABLE
    scope: MetricScope = MetricScope.CONSOLIDATED
    qualitative_statement: NonEmptyStr | None = None
    missing_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def shape_matches_guidance_type(self) -> "GuidanceValue":
        numeric = (self.low, self.high, self.point)
        if self.type is GuidanceType.RANGE:
            if self.low is None or self.high is None or self.point is not None:
                raise ValueError("range guidance requires low and high, and no point")
            if self.low > self.high:
                raise ValueError("range guidance low cannot exceed high")
        elif self.type in {GuidanceType.POINT, GuidanceType.FLOOR, GuidanceType.CEILING}:
            if self.point is None or self.low is not None or self.high is not None:
                raise ValueError(f"{self.type.value} guidance requires point only")
        else:
            if any(value is not None for value in numeric):
                raise ValueError("qualitative guidance cannot carry numeric values")
            if self.qualitative_statement is None:
                raise ValueError("qualitative guidance requires qualitative_statement")
        if self.type is not GuidanceType.QUALITATIVE and self.qualitative_statement is not None:
            raise ValueError("numeric guidance cannot carry qualitative_statement")
        return self

    @property
    def midpoint(self) -> Decimal | None:
        if self.type is GuidanceType.RANGE:
            assert self.low is not None and self.high is not None
            return (self.low + self.high) / Decimal("2")
        return self.point


class GuidanceEvaluation(_GuidanceModel):
    """Deterministic comparison result; null deltas carry an explicit reason."""

    status: GuidanceStatus
    delta_vs_low: FiniteDecimal | None = None
    delta_vs_mid: FiniteDecimal | None = None
    delta_vs_high: FiniteDecimal | None = None
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def nulls_are_explained(self) -> "GuidanceEvaluation":
        deltas = (self.delta_vs_low, self.delta_vs_mid, self.delta_vs_high)
        if self.status in {GuidanceStatus.PENDING, GuidanceStatus.INCOMPARABLE}:
            if any(delta is not None for delta in deltas):
                raise ValueError("pending or incomparable guidance cannot carry deltas")
            if self.reason is None:
                raise ValueError("pending or incomparable guidance requires a reason")
        elif self.reason is not None:
            raise ValueError("evaluated guidance cannot carry a reason")
        return self


class GuidanceEntry(_GuidanceModel):
    """A management statement and, when available, its eventual actual."""

    guidance_id: NonEmptyStr
    issued_fiscal_period: FiscalPeriod
    issued_at: AwareDatetime | None = None
    target_fiscal_period: FiscalPeriod
    metric: NormalizedMetric
    guidance: GuidanceValue
    eventual_actual: MetricValue | None = None
    eventual_actual_missing_reason: NonEmptyStr | None = None
    evidence_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)
    actual_evidence_refs: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def target_and_actual_are_coherent(self) -> "GuidanceEntry":
        if self.eventual_actual is None:
            if self.eventual_actual_missing_reason is None:
                raise ValueError("missing eventual actual requires eventual_actual_missing_reason")
            if self.actual_evidence_refs:
                raise ValueError("actual evidence refs require an eventual actual")
        else:
            if self.eventual_actual_missing_reason is not None:
                raise ValueError("actual missing reason requires a missing eventual actual")
            if not self.actual_evidence_refs:
                raise ValueError("eventual actual requires actual_evidence_refs")
            if self.eventual_actual.metric is not self.metric:
                raise ValueError("eventual actual metric must match guidance metric")
            if self.eventual_actual.fiscal_period != self.target_fiscal_period:
                raise ValueError("eventual actual fiscal period must match target fiscal period")
        return self

    @property
    def evaluation(self) -> "GuidanceEvaluation":
        """Return the deterministic, evidence-backed outcome for this entry."""
        return evaluate_guidance(self)


class GuidanceBehavior(_GuidanceModel):
    """Metric-specific historical behavior, never an assertion of intent."""

    metric: NormalizedMetric
    observations: int
    beat_high_end_count: int = 0
    met_range_count: int = 0
    miss_low_end_count: int = 0
    incomparable_count: int = 0
    pending_count: int = 0
    mean_delta_vs_mid: FiniteDecimal | None = None
    median_delta_vs_mid: FiniteDecimal | None = None
    interpretation: str | None = None
    confidence: str = "unknown"
    supporting_guidance_ids: tuple[NonEmptyStr, ...] = ()
    counterexample_guidance_ids: tuple[NonEmptyStr, ...] = ()


def _converted_actual(entry: GuidanceEntry) -> tuple[Decimal | None, str | None]:
    actual = entry.eventual_actual
    if actual is None or actual.value is None:
        return None, "actual_not_available"
    guide = entry.guidance
    if actual.growth_basis is not guide.growth_basis:
        return None, "growth_basis_mismatch"
    if actual.accounting_basis is not guide.accounting_basis:
        return None, "accounting_basis_mismatch"
    if actual.scope is not guide.scope:
        return None, "scope_mismatch"
    if actual.unit is guide.unit:
        return actual.value, None
    try:
        return convert_scale(actual.value, actual.unit, guide.unit), None
    except ValueError:
        return None, "unit_mismatch"


def evaluate_guidance(entry: GuidanceEntry) -> GuidanceEvaluation:
    """Evaluate one ledger entry without guessing comparability or intent."""
    actual, reason = _converted_actual(entry)
    if actual is None:
        status = GuidanceStatus.PENDING if reason == "actual_not_available" else GuidanceStatus.INCOMPARABLE
        return GuidanceEvaluation(status=status, reason=reason)
    guidance = entry.guidance
    if guidance.type is GuidanceType.QUALITATIVE:
        return GuidanceEvaluation(status=GuidanceStatus.INCOMPARABLE, reason="qualitative_guidance")

    midpoint = guidance.midpoint
    assert midpoint is not None
    low = guidance.low if guidance.type is GuidanceType.RANGE else guidance.point
    high = guidance.high if guidance.type is GuidanceType.RANGE else guidance.point
    assert low is not None and high is not None
    common = {"delta_vs_low": actual - low, "delta_vs_mid": actual - midpoint, "delta_vs_high": actual - high}
    if guidance.type is GuidanceType.RANGE:
        status = GuidanceStatus.BEAT if actual > high else GuidanceStatus.MISS if actual < low else GuidanceStatus.MET
    elif guidance.type in {GuidanceType.POINT, GuidanceType.FLOOR}:
        status = GuidanceStatus.BEAT if actual > midpoint else GuidanceStatus.MISS if actual < midpoint else GuidanceStatus.MET
    else:  # ceiling: lower is better
        status = GuidanceStatus.BEAT if actual < midpoint else GuidanceStatus.MISS if actual > midpoint else GuidanceStatus.MET
    return GuidanceEvaluation(status=status, **common)


def summarize_guidance_behavior(
    entries: Iterable[GuidanceEntry], *, minimum_observations: int = 4
) -> tuple[GuidanceBehavior, ...]:
    """Summarize deterministic outcomes by metric.

    ``historically_conservative`` is intentionally gated on an explicit sample
    and at least one counterexample; it is a descriptive label, not an intent
    claim.  Qualitative, pending, and incomparable entries never fabricate a
    numeric observation.
    """
    if minimum_observations < 1:
        raise ValueError("minimum_observations must be positive")
    grouped: dict[NormalizedMetric, list[GuidanceEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.metric, []).append(entry)
    results: list[GuidanceBehavior] = []
    for metric in sorted(grouped, key=str):
        group = grouped[metric]
        evaluations = [(entry, evaluate_guidance(entry)) for entry in group]
        evaluated = [(entry, evaluation) for entry, evaluation in evaluations if evaluation.status in {GuidanceStatus.BEAT, GuidanceStatus.MET, GuidanceStatus.MISS}]
        beats = [entry for entry, evaluation in evaluated if evaluation.status is GuidanceStatus.BEAT]
        counters = [entry for entry, evaluation in evaluated if evaluation.status is not GuidanceStatus.BEAT]
        deltas = [evaluation.delta_vs_mid for _, evaluation in evaluated if evaluation.delta_vs_mid is not None]
        enough = len(evaluated) >= minimum_observations
        conservative = enough and bool(counters) and len(beats) > len(counters)
        results.append(GuidanceBehavior(
            metric=metric,
            observations=len(evaluated),
            beat_high_end_count=len(beats),
            met_range_count=sum(evaluation.status is GuidanceStatus.MET for _, evaluation in evaluated),
            miss_low_end_count=sum(evaluation.status is GuidanceStatus.MISS for _, evaluation in evaluated),
            incomparable_count=sum(evaluation.status is GuidanceStatus.INCOMPARABLE for _, evaluation in evaluations),
            pending_count=sum(evaluation.status is GuidanceStatus.PENDING for _, evaluation in evaluations),
            mean_delta_vs_mid=(sum(deltas) / len(deltas)) if deltas else None,
            median_delta_vs_mid=median(deltas) if deltas else None,
            interpretation="historically_conservative" if conservative else None,
            confidence="medium" if conservative else "unknown",
            supporting_guidance_ids=tuple(entry.guidance_id for entry in beats),
            counterexample_guidance_ids=tuple(entry.guidance_id for entry in counters),
        ))
    return tuple(results)
