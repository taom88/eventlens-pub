from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AccountingBasis,
    FiscalPeriod,
    GrowthBasis,
    GuidanceEntry,
    GuidanceStatus,
    GuidanceType,
    GuidanceValue,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
    evaluate_guidance,
    summarize_guidance_behavior,
)


PERIOD = FiscalPeriod(fiscal_year=2026, fiscal_quarter=2)


def _entry(
    guidance: GuidanceValue,
    actual: str | None = "42",
    **changes: object,
) -> GuidanceEntry:
    payload: dict[str, object] = {
        "guidance_id": "demo-fy26-q2-cloud",
        "issued_fiscal_period": FiscalPeriod(fiscal_year=2026, fiscal_quarter=1),
        "issued_at": datetime(2025, 10, 29, tzinfo=timezone.utc),
        "target_fiscal_period": PERIOD,
        "metric": NormalizedMetric.GROWTH,
        "guidance": guidance,
        "eventual_actual": None if actual is None else MetricValue(
            metric=NormalizedMetric.GROWTH,
            value=Decimal(actual),
            unit=guidance.unit,
            fiscal_period=PERIOD,
            accounting_basis=guidance.accounting_basis,
            growth_basis=guidance.growth_basis,
            scope=guidance.scope,
        ),
        "eventual_actual_missing_reason": "quarter_not_reported" if actual is None else None,
        "evidence_refs": ("receipt:guidance",),
        "actual_evidence_refs": () if actual is None else ("receipt:actual",),
    }
    payload.update(changes)
    return GuidanceEntry(**payload)


def _range() -> GuidanceValue:
    return GuidanceValue(
        type=GuidanceType.RANGE,
        low=Decimal("40"),
        high=Decimal("41"),
        unit=MetricUnit.PERCENT,
        growth_basis=GrowthBasis.CONSTANT_CURRENCY,
    )


@pytest.mark.parametrize(
    ("guidance", "actual", "status"),
    [
        (_range(), "42", GuidanceStatus.BEAT),
        (_range(), "40.5", GuidanceStatus.MET),
        (_range(), "39", GuidanceStatus.MISS),
        (GuidanceValue(type=GuidanceType.POINT, point=Decimal("40"), unit=MetricUnit.PERCENT), "40", GuidanceStatus.MET),
        (GuidanceValue(type=GuidanceType.FLOOR, point=Decimal("40"), unit=MetricUnit.PERCENT), "41", GuidanceStatus.BEAT),
        (GuidanceValue(type=GuidanceType.CEILING, point=Decimal("40"), unit=MetricUnit.PERCENT), "39", GuidanceStatus.BEAT),
    ],
)
def test_numeric_guidance_forms_have_deterministic_outcomes(
    guidance: GuidanceValue, actual: str, status: GuidanceStatus
) -> None:
    evaluation = evaluate_guidance(_entry(guidance, actual))
    assert evaluation.status is status
    assert evaluation.delta_vs_mid is not None


def test_range_deltas_preserve_low_mid_high_meaning() -> None:
    evaluation = _entry(_range(), "42").evaluation
    assert evaluation.delta_vs_low == Decimal("2")
    assert evaluation.delta_vs_mid == Decimal("1.5")
    assert evaluation.delta_vs_high == Decimal("1")


def test_qualitative_and_missing_actual_do_not_fabricate_numeric_evaluations() -> None:
    qualitative = GuidanceValue(
        type=GuidanceType.QUALITATIVE,
        unit=MetricUnit.PERCENT,
        qualitative_statement="growth should remain strong",
    )
    assert evaluate_guidance(_entry(qualitative)).status is GuidanceStatus.INCOMPARABLE
    pending = evaluate_guidance(_entry(_range(), actual=None))
    assert pending.status is GuidanceStatus.PENDING
    assert pending.reason == "actual_not_available"


def test_reported_actual_cannot_be_compared_with_constant_currency_guidance() -> None:
    entry = _entry(
        _range(),
        eventual_actual=MetricValue(
            metric=NormalizedMetric.GROWTH,
            value=Decimal("42"),
            unit=MetricUnit.PERCENT,
            fiscal_period=PERIOD,
            accounting_basis=AccountingBasis.NOT_APPLICABLE,
            growth_basis=GrowthBasis.REPORTED,
            scope=MetricScope.CONSOLIDATED,
        ),
    )
    evaluation = evaluate_guidance(entry)
    assert evaluation.status is GuidanceStatus.INCOMPARABLE
    assert evaluation.reason == "growth_basis_mismatch"


def test_entry_requires_actual_and_guidance_evidence_and_target_period() -> None:
    with pytest.raises(ValidationError, match="actual_evidence_refs"):
        _entry(_range(), actual_evidence_refs=())
    with pytest.raises(ValidationError, match="target fiscal period"):
        _entry(
            _range(),
            eventual_actual=MetricValue(
                metric=NormalizedMetric.GROWTH,
                value=Decimal("42"),
                unit=MetricUnit.PERCENT,
                fiscal_period=FiscalPeriod(fiscal_year=2026, fiscal_quarter=3),
                accounting_basis=AccountingBasis.NOT_APPLICABLE,
                growth_basis=GrowthBasis.CONSTANT_CURRENCY,
                scope=MetricScope.CONSOLIDATED,
            ),
        )


def test_historically_conservative_requires_explicit_sample_and_counterexamples() -> None:
    three_beats = tuple(
        _entry(_range(), "42", guidance_id=f"g-{index}") for index in range(3)
    )
    too_small = summarize_guidance_behavior(three_beats)
    assert too_small[0].interpretation is None

    four_beats = three_beats + (_entry(_range(), "42", guidance_id="g-4"),)
    no_counterexample = summarize_guidance_behavior(four_beats)
    assert no_counterexample[0].interpretation is None

    behavior = summarize_guidance_behavior(
        four_beats + (_entry(_range(), "40.5", guidance_id="g-counterexample"),)
    )[0]
    assert behavior.interpretation == "historically_conservative"
    assert behavior.counterexample_guidance_ids == ("g-counterexample",)
