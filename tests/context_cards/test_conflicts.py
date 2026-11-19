from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AccountingBasis,
    ConflictMateriality,
    ConflictResolutionRule,
    FiscalPeriod,
    GrowthBasis,
    MetricObservation,
    MetricScope,
    MetricTier,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
    ObservationClaimType,
    ObservationPrecision,
    ResolutionStatus,
    context_card_json_schema,
    detect_conflicts,
    resolve_basis_difference,
)


PERIOD = FiscalPeriod(fiscal_year=2026, fiscal_quarter=2)


def _observation(
    observation_id: str,
    value: str,
    *,
    growth_basis: GrowthBasis = GrowthBasis.REPORTED,
    accounting_basis: AccountingBasis = AccountingBasis.NOT_APPLICABLE,
    claim_type: ObservationClaimType = ObservationClaimType.ACTUAL,
    period: FiscalPeriod = PERIOD,
    tier: MetricTier = MetricTier.TIER_1,
    precision: ObservationPrecision = ObservationPrecision.EXACT,
    rounding_decimal_places: int | None = None,
    scope: MetricScope = MetricScope.PRODUCT,
) -> MetricObservation:
    return MetricObservation(
        id=observation_id,
        kpi_id="cloud_growth_reported",
        metric_tier=tier,
        claim_type=claim_type,
        precision=precision,
        rounding_decimal_places=rounding_decimal_places,
        metric=MetricValue(
            metric=NormalizedMetric.GROWTH,
            value=Decimal(value),
            unit=MetricUnit.PERCENT,
            fiscal_period=period,
            accounting_basis=accounting_basis,
            growth_basis=growth_basis,
            scope=scope,
        ),
        claim_id=f"claim_{observation_id}",
        evidence_receipt_id=f"claim_{observation_id}",
        source_ref=f"source:{observation_id}:passage:p1",
    )


def test_reported_42_and_constant_currency_43_are_resolved_as_distinct_bases() -> None:
    reported = _observation("reported", "42")
    constant_currency = _observation(
        "constant_currency", "43", growth_basis=GrowthBasis.CONSTANT_CURRENCY
    )

    direct = resolve_basis_difference(reported, constant_currency)
    conflicts = detect_conflicts((reported, constant_currency))

    assert direct is not None
    assert direct.rule is ConflictResolutionRule.BASIS_DIFFERENCE
    assert direct.status is ResolutionStatus.RESOLVED
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.resolution_status is ResolutionStatus.RESOLVED
    assert conflict.review_blocking is False
    assert conflict.observations == (constant_currency, reported)
    assert set(conflict.resolution.provenance_refs) == {
        "claim_reported",
        "claim_constant_currency",
    }
    assert not hasattr(conflict.resolution, "winner_observation_id")


def test_same_basis_same_period_material_difference_remains_unresolved_and_blocking() -> None:
    first = _observation("issuer", "42")
    second = _observation("transcript", "44")

    conflict = detect_conflicts((first, second))[0]

    assert conflict.materiality is ConflictMateriality.MATERIAL
    assert conflict.resolution_status is ResolutionStatus.UNRESOLVED
    assert conflict.resolution.rule is ConflictResolutionRule.UNRESOLVED_DIFFERENCE
    assert conflict.resolution.human_review_required is True
    assert conflict.review_blocking is True
    assert {item.metric.value for item in conflict.observations} == {
        Decimal("42"),
        Decimal("44"),
    }


def test_approximate_guidance_and_actual_remain_distinct_semantic_facts() -> None:
    guidance = _observation(
        "guidance",
        "40",
        claim_type=ObservationClaimType.MANAGEMENT_GUIDANCE,
        precision=ObservationPrecision.APPROXIMATE,
    )
    actual = _observation("actual", "39")

    conflict = detect_conflicts((guidance, actual))[0]

    assert conflict.resolution_status is ResolutionStatus.RESOLVED
    assert conflict.resolution.rule is ConflictResolutionRule.CLAIM_TYPE_DIFFERENCE
    assert conflict.review_blocking is False
    assert {item.claim_type for item in conflict.observations} == {
        ObservationClaimType.MANAGEMENT_GUIDANCE,
        ObservationClaimType.ACTUAL,
    }


def test_period_and_explicit_rounding_differences_are_deterministically_resolved() -> None:
    prior = _observation(
        "prior", "40", period=FiscalPeriod(fiscal_year=2026, fiscal_quarter=1)
    )
    current = _observation("current", "39")
    period_conflict = detect_conflicts((prior, current))[0]
    assert period_conflict.resolution.rule is ConflictResolutionRule.PERIOD_DIFFERENCE

    rounded = _observation(
        "rounded",
        "42",
        precision=ObservationPrecision.ROUNDED,
        rounding_decimal_places=0,
    )
    exact = _observation("exact", "42.4")
    rounding_conflict = detect_conflicts((rounded, exact))[0]
    assert rounding_conflict.resolution.rule is ConflictResolutionRule.ROUNDING_DIFFERENCE


def test_nonoverlapping_known_scopes_and_equivalent_values_are_not_conflicts() -> None:
    product = _observation("product", "42", scope=MetricScope.PRODUCT)
    segment = _observation("segment", "43", scope=MetricScope.SEGMENT)
    equivalent = _observation("equivalent", "42")

    assert detect_conflicts((product, segment)) == ()
    assert detect_conflicts((product, equivalent)) == ()


def test_unknown_basis_does_not_enable_automatic_basis_resolution() -> None:
    unknown = _observation("unknown", "42", growth_basis=GrowthBasis.UNKNOWN)
    reported = _observation("reported", "43")

    assert resolve_basis_difference(unknown, reported) is None
    assert detect_conflicts((unknown, reported))[0].resolution_status is ResolutionStatus.UNRESOLVED


def test_observations_are_strict_numeric_and_duplicate_ids_fail_closed() -> None:
    payload = _observation("missing", "42").model_dump()
    payload["metric"]["value"] = None
    with pytest.raises(ValidationError, match="explicit numeric value"):
        MetricObservation.model_validate(payload)

    duplicate = _observation("duplicate", "42")
    with pytest.raises(ValueError, match="must be unique"):
        detect_conflicts((duplicate, duplicate))


def test_conflict_types_are_exposed_on_canonical_v4_schema_surface() -> None:
    schema = context_card_json_schema()

    assert "conflicts" in schema["properties"]
    assert "Conflict" in schema["$defs"]
    assert "MetricObservation" in schema["$defs"]
    assert "ConflictResolution" in schema["$defs"]
