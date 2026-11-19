"""Deterministic contradiction detection for canonical metric observations.

Conflict handling is deliberately non-destructive: every differing observation
is retained, and an explainable difference is classified rather than collapsed
to a preferred value.  Unknown or genuinely contradictory facts remain
unresolved for human review.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum
import hashlib
from itertools import combinations
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .metrics import (
    AccountingBasis,
    GrowthBasis,
    MetricScope,
    MetricUnit,
    MetricValue,
    convert_scale,
)
from .ontology import MetricTier


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CanonicalId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"),
]


class _ConflictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True
    )


class ObservationClaimType(StrEnum):
    """Semantic role of a numeric claim; unlike values are not interchangeable."""

    ACTUAL = "actual"
    MANAGEMENT_GUIDANCE = "management_guidance"
    SELL_SIDE_CONSENSUS = "sell_side_consensus"
    WHISPER = "whisper"
    DERIVED = "derived"
    OTHER = "other"


class ObservationPrecision(StrEnum):
    """Source-declared numeric precision, never guessed from prose."""

    EXACT = "exact"
    ROUNDED = "rounded"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


class ConflictMateriality(StrEnum):
    MATERIAL = "material"
    NON_MATERIAL = "non_material"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ConflictResolutionRule(StrEnum):
    CLAIM_TYPE_DIFFERENCE = "claim_type_difference"
    PERIOD_DIFFERENCE = "period_difference"
    BASIS_DIFFERENCE = "basis_difference"
    ROUNDING_DIFFERENCE = "rounding_difference"
    UNRESOLVED_DIFFERENCE = "unresolved_difference"


class MetricObservation(_ConflictModel):
    """One immutable, provenance-backed observation of an ontology KPI."""

    id: NonEmptyStr
    kpi_id: CanonicalId
    metric_tier: MetricTier
    claim_type: ObservationClaimType
    precision: ObservationPrecision = ObservationPrecision.EXACT
    rounding_decimal_places: int | None = Field(default=None, ge=0)
    metric: MetricValue
    claim_id: NonEmptyStr
    evidence_receipt_id: NonEmptyStr
    source_ref: NonEmptyStr

    @model_validator(mode="after")
    def observation_is_numeric_and_precision_is_explicit(self) -> "MetricObservation":
        if self.metric.value is None:
            raise ValueError("a conflict observation must carry an explicit numeric value")
        if self.claim_id != self.evidence_receipt_id:
            raise ValueError(
                "claim_id must match evidence_receipt_id for a canonical semantic claim"
            )
        if self.precision is ObservationPrecision.ROUNDED:
            if self.rounding_decimal_places is None:
                raise ValueError("a rounded observation requires rounding_decimal_places")
        elif self.rounding_decimal_places is not None:
            raise ValueError("rounding_decimal_places applies only to rounded observations")
        return self


class ConflictResolution(_ConflictModel):
    """Auditable classification of a difference, without a winning value."""

    status: ResolutionStatus
    rule: ConflictResolutionRule
    explanation: NonEmptyStr
    observation_ids: tuple[NonEmptyStr, ...] = Field(min_length=2)
    provenance_refs: tuple[NonEmptyStr, ...] = Field(min_length=2)
    human_review_required: bool

    @model_validator(mode="after")
    def resolution_is_coherent(self) -> "ConflictResolution":
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("resolution observation_ids must be unique")
        if len(set(self.provenance_refs)) != len(self.provenance_refs):
            raise ValueError("resolution provenance_refs must be unique")
        if self.status is ResolutionStatus.RESOLVED and self.human_review_required:
            raise ValueError("a deterministic resolved difference cannot require human review")
        if self.status is ResolutionStatus.UNRESOLVED and not self.human_review_required:
            raise ValueError("an unresolved difference requires human review")
        if (
            self.status is ResolutionStatus.UNRESOLVED
            and self.rule is not ConflictResolutionRule.UNRESOLVED_DIFFERENCE
        ):
            raise ValueError("an unresolved difference must use the unresolved rule")
        if (
            self.status is ResolutionStatus.RESOLVED
            and self.rule is ConflictResolutionRule.UNRESOLVED_DIFFERENCE
        ):
            raise ValueError("a resolved difference must identify its deterministic rule")
        return self


class Conflict(_ConflictModel):
    """A pair of differing observations and its explicit resolution state."""

    id: NonEmptyStr
    kpi_id: CanonicalId
    observations: tuple[MetricObservation, MetricObservation]
    materiality: ConflictMateriality
    resolution_status: ResolutionStatus
    resolution: ConflictResolution
    review_blocking: bool

    @model_validator(mode="after")
    def conflict_is_coherent(self) -> "Conflict":
        observation_ids = tuple(observation.id for observation in self.observations)
        if len(set(observation_ids)) != 2:
            raise ValueError("a conflict requires two distinct observations")
        if any(observation.kpi_id != self.kpi_id for observation in self.observations):
            raise ValueError("conflict observations must match conflict kpi_id")
        if self.resolution_status is not self.resolution.status:
            raise ValueError("resolution_status must match resolution.status")
        if set(self.resolution.observation_ids) != set(observation_ids):
            raise ValueError("resolution must record every conflict observation")
        provenance = {observation.evidence_receipt_id for observation in self.observations}
        if set(self.resolution.provenance_refs) != provenance:
            raise ValueError("resolution provenance must record every observation receipt")
        expected_materiality = _materiality(*self.observations)
        if self.materiality is not expected_materiality:
            raise ValueError("conflict materiality must be derived from its KPI tiers")
        expected_blocking = (
            self.materiality is ConflictMateriality.MATERIAL
            and self.resolution_status is ResolutionStatus.UNRESOLVED
        )
        if self.review_blocking != expected_blocking:
            raise ValueError("only an unresolved material conflict is review blocking")
        if self.id != _conflict_id(*self.observations):
            raise ValueError("conflict id must be deterministically derived from its observations")
        if self.resolution != _resolve_difference(*self.observations):
            raise ValueError("conflict resolution must match the deterministic rule")
        return self


def resolve_basis_difference(
    first: MetricObservation,
    second: MetricObservation,
) -> ConflictResolution | None:
    """Classify a known accounting or growth-basis difference.

    ``None`` means no deterministic basis explanation is available.  In
    particular, an unknown basis is not evidence of a basis difference.
    """

    if (
        first.kpi_id != second.kpi_id
        or first.metric.metric is not second.metric.metric
        or _period_identity(first.metric) != _period_identity(second.metric)
        or first.metric.scope is not second.metric.scope
        or _comparable_values(first.metric, second.metric) is None
    ):
        return None

    accounting_difference = _known_difference(
        first.metric.accounting_basis,
        second.metric.accounting_basis,
        excluded={AccountingBasis.UNKNOWN, AccountingBasis.NOT_APPLICABLE},
    )
    growth_difference = _known_difference(
        first.metric.growth_basis,
        second.metric.growth_basis,
        excluded={GrowthBasis.UNKNOWN, GrowthBasis.NOT_APPLICABLE},
    )
    if not accounting_difference and not growth_difference:
        return None
    dimensions: list[str] = []
    if accounting_difference:
        dimensions.append("accounting")
    if growth_difference:
        dimensions.append("growth")
    return _resolution(
        first,
        second,
        status=ResolutionStatus.RESOLVED,
        rule=ConflictResolutionRule.BASIS_DIFFERENCE,
        explanation=(
            "Observations remain distinct because their known "
            + " and ".join(dimensions)
            + " bases differ; neither value was selected as a winner."
        ),
    )


def detect_conflicts(observations: Iterable[MetricObservation]) -> tuple[Conflict, ...]:
    """Detect differing observations and classify only deterministic exceptions.

    Comparisons are pairwise and stable.  Known non-overlapping scopes are not
    contradictions.  Unknown scope, basis, or precision never authorizes an
    automatic resolution.
    """

    items = tuple(observations)
    ids = tuple(observation.id for observation in items)
    if len(set(ids)) != len(ids):
        raise ValueError("metric observation ids must be unique")

    conflicts: list[Conflict] = []
    for first, second in combinations(sorted(items, key=lambda item: item.id), 2):
        if first.kpi_id != second.kpi_id:
            continue
        if not _scopes_may_overlap(first.metric.scope, second.metric.scope):
            continue
        if _values_equal(first.metric, second.metric):
            continue

        resolution = _resolve_difference(first, second)
        materiality = _materiality(first, second)
        conflict_id = _conflict_id(first, second)
        conflicts.append(
            Conflict(
                id=conflict_id,
                kpi_id=first.kpi_id,
                observations=(first, second),
                materiality=materiality,
                resolution_status=resolution.status,
                resolution=resolution,
                review_blocking=(
                    materiality is ConflictMateriality.MATERIAL
                    and resolution.status is ResolutionStatus.UNRESOLVED
                ),
            )
        )
    return tuple(conflicts)


def _resolve_difference(
    first: MetricObservation,
    second: MetricObservation,
) -> ConflictResolution:
    if (
        first.metric.metric is not second.metric.metric
        or first.metric.scope is not second.metric.scope
        or _comparable_values(first.metric, second.metric) is None
    ):
        return _unresolved_resolution(first, second)
    if first.claim_type is not second.claim_type:
        return _resolution(
            first,
            second,
            status=ResolutionStatus.RESOLVED,
            rule=ConflictResolutionRule.CLAIM_TYPE_DIFFERENCE,
            explanation=(
                "Observations are distinct semantic facts with different claim types; "
                "neither value was selected as a winner."
            ),
        )
    if _period_identity(first.metric) != _period_identity(second.metric):
        return _resolution(
            first,
            second,
            status=ResolutionStatus.RESOLVED,
            rule=ConflictResolutionRule.PERIOD_DIFFERENCE,
            explanation=(
                "Observations refer to different fiscal periods; neither value was "
                "selected as a winner."
            ),
        )
    basis_resolution = resolve_basis_difference(first, second)
    if basis_resolution is not None:
        return basis_resolution
    if _rounding_compatible(first, second):
        return _resolution(
            first,
            second,
            status=ResolutionStatus.RESOLVED,
            rule=ConflictResolutionRule.ROUNDING_DIFFERENCE,
            explanation=(
                "The exact observation lies within the explicitly declared rounding "
                "interval; neither value was selected as a winner."
            ),
        )
    return _unresolved_resolution(first, second)


def _unresolved_resolution(
    first: MetricObservation,
    second: MetricObservation,
) -> ConflictResolution:
    return _resolution(
        first,
        second,
        status=ResolutionStatus.UNRESOLVED,
        rule=ConflictResolutionRule.UNRESOLVED_DIFFERENCE,
        explanation=(
            "Same-KPI observations differ without a deterministic claim-type, period, "
            "basis, or rounding explanation; human review is required."
        ),
    )


def _resolution(
    first: MetricObservation,
    second: MetricObservation,
    *,
    status: ResolutionStatus,
    rule: ConflictResolutionRule,
    explanation: str,
) -> ConflictResolution:
    return ConflictResolution(
        status=status,
        rule=rule,
        explanation=explanation,
        observation_ids=(first.id, second.id),
        provenance_refs=(first.evidence_receipt_id, second.evidence_receipt_id),
        human_review_required=status is ResolutionStatus.UNRESOLVED,
    )


def _known_difference(
    first: StrEnum,
    second: StrEnum,
    *,
    excluded: set[StrEnum],
) -> bool:
    return first is not second and first not in excluded and second not in excluded


def _period_identity(metric: MetricValue) -> tuple[int, int | None]:
    period = metric.fiscal_period
    return period.fiscal_year, period.fiscal_quarter


def _scopes_may_overlap(first: MetricScope, second: MetricScope) -> bool:
    if first is second:
        return True
    ambiguous = {MetricScope.UNKNOWN, MetricScope.NOT_APPLICABLE}
    return first in ambiguous or second in ambiguous


def _value_in_percent(metric: MetricValue) -> Decimal | None:
    assert metric.value is not None
    if metric.unit is MetricUnit.PERCENT:
        return metric.value
    if metric.unit is MetricUnit.RATIO:
        return metric.value * Decimal("100")
    return None


def _comparable_values(first: MetricValue, second: MetricValue) -> tuple[Decimal, Decimal] | None:
    assert first.value is not None and second.value is not None
    if first.unit is second.unit:
        return first.value, second.value
    first_percent = _value_in_percent(first)
    second_percent = _value_in_percent(second)
    if first_percent is not None and second_percent is not None:
        return first_percent, second_percent
    try:
        return first.value, convert_scale(second.value, second.unit, first.unit)  # type: ignore[arg-type]
    except ValueError:
        return None


def _values_equal(first: MetricValue, second: MetricValue) -> bool:
    if first.metric is not second.metric:
        return False
    values = _comparable_values(first, second)
    return values is not None and values[0] == values[1]


def _rounding_compatible(first: MetricObservation, second: MetricObservation) -> bool:
    values = _comparable_values(first.metric, second.metric)
    if values is None:
        return False
    allowed = {ObservationPrecision.EXACT, ObservationPrecision.ROUNDED}
    if first.precision not in allowed or second.precision not in allowed:
        return False
    if (
        first.precision is not ObservationPrecision.ROUNDED
        and second.precision is not ObservationPrecision.ROUNDED
    ):
        return False

    def tolerance(observation: MetricObservation) -> Decimal:
        if observation.precision is ObservationPrecision.EXACT:
            return Decimal("0")
        assert observation.rounding_decimal_places is not None
        return Decimal("0.5").scaleb(-observation.rounding_decimal_places)

    return abs(values[0] - values[1]) <= tolerance(first) + tolerance(second)


def _materiality(
    first: MetricObservation,
    second: MetricObservation,
) -> ConflictMateriality:
    return (
        ConflictMateriality.MATERIAL
        if {first.metric_tier, second.metric_tier} & {MetricTier.TIER_1, MetricTier.TIER_2}
        else ConflictMateriality.NON_MATERIAL
    )


def _conflict_id(first: MetricObservation, second: MetricObservation) -> str:
    digest = hashlib.sha256(
        f"{first.kpi_id}\0{first.id}\0{second.id}".encode("utf-8")
    ).hexdigest()[:20]
    return f"conflict_{digest}"


__all__ = [
    "Conflict",
    "ConflictMateriality",
    "ConflictResolution",
    "ConflictResolutionRule",
    "MetricObservation",
    "ObservationClaimType",
    "ObservationPrecision",
    "ResolutionStatus",
    "detect_conflicts",
    "resolve_basis_difference",
]
