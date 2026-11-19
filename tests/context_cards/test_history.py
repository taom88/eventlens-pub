from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    ActualResults,
    AccountingBasis,
    ConsensusType,
    Expectation,
    FiscalPeriod,
    GuidanceEntry,
    GuidanceType,
    GuidanceValue,
    HistoricalPattern,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
    SurpriseLayers,
    assemble_quarter,
    historical_weight,
    latest_four_completed,
)


def _period(year: int, quarter: int) -> FiscalPeriod:
    return FiscalPeriod(fiscal_year=year, fiscal_quarter=quarter)


def _revenue(period: FiscalPeriod, value: str) -> MetricValue:
    return MetricValue(
        metric=NormalizedMetric.REVENUE,
        value=Decimal(value),
        unit=MetricUnit.USD_BILLION,
        fiscal_period=period,
        accounting_basis=AccountingBasis.NOT_APPLICABLE,
        growth_basis="not_applicable",
        scope=MetricScope.CONSOLIDATED,
    )


def _actuals(period: FiscalPeriod) -> ActualResults:
    return ActualResults(
        fiscal_period=period,
        metrics={NormalizedMetric.REVENUE: _revenue(period, "10")},
        evidence_refs=("actual-receipt",),
    )


def _quarter(period: FiscalPeriod):
    return assemble_quarter(
        fiscal_period=period,
        expectations=(
            Expectation(
                consensus_type=ConsensusType.SELL_SIDE,
                fiscal_period=period,
                metrics={NormalizedMetric.REVENUE: _revenue(period, "9")},
                evidence_refs=("consensus-receipt",),
            ),
        ),
        actual_results=_actuals(period),
        surprise_layers=SurpriseLayers(),
        reaction_missing_reason="not_populated_in_ticket_10",
        evidence_refs=("quarter-receipt",),
    )


def test_explicit_fiscal_order_selects_latest_four_completed() -> None:
    quarters = [_quarter(_period(2025, quarter)) for quarter in (1, 2, 3, 4)]
    quarters.append(_quarter(_period(2026, 1)))

    selected = latest_four_completed(reversed(quarters))

    assert [quarter.fiscal_period.label for quarter in selected] == [
        "FY2026_Q1", "FY2025_Q4", "FY2025_Q3", "FY2025_Q2"
    ]


def test_prior_guidance_must_target_correct_quarter_and_stays_distinct() -> None:
    target = _period(2025, 2)
    entry = GuidanceEntry(
        guidance_id="guidance-q2",
        issued_fiscal_period=_period(2025, 1),
        target_fiscal_period=target,
        metric=NormalizedMetric.REVENUE,
        guidance=GuidanceValue(type=GuidanceType.POINT, point=Decimal("9"), unit=MetricUnit.USD_BILLION),
        evidence_refs=("guidance-receipt",),
        eventual_actual=_revenue(target, "10"),
        actual_evidence_refs=("actual-receipt",),
    )
    expectation = Expectation(
        consensus_type=ConsensusType.PRIOR_MANAGEMENT_GUIDANCE,
        fiscal_period=target,
        prior_guidance=(entry,),
    )
    assert expectation.prior_guidance == (entry,)

    with pytest.raises(ValidationError, match="cannot substitute"):
        Expectation(
            consensus_type=ConsensusType.PRIOR_MANAGEMENT_GUIDANCE,
            fiscal_period=target,
            metrics={NormalizedMetric.REVENUE: _revenue(target, "9")},
            prior_guidance=(entry,),
        )
    with pytest.raises(ValidationError, match="must target"):
        Expectation(
            consensus_type=ConsensusType.PRIOR_MANAGEMENT_GUIDANCE,
            fiscal_period=_period(2025, 3),
            prior_guidance=(entry,),
        )


def test_missing_metrics_remain_null_with_explicit_reason() -> None:
    period = _period(2025, 2)
    actuals = ActualResults(
        fiscal_period=period,
        metrics={NormalizedMetric.REVENUE: None},
        missing_metric_reasons={NormalizedMetric.REVENUE: "issuer did not disclose this metric"},
        evidence_refs=("actual-receipt",),
    )
    assert actuals.metrics[NormalizedMetric.REVENUE] is None
    assert actuals.missing_metric_reasons[NormalizedMetric.REVENUE]

    with pytest.raises(ValidationError, match="only permit"):
        ActualResults(
            fiscal_period=period,
            metrics={NormalizedMetric.REVENUE: _revenue(period, "10")},
            missing_metric_reasons={NormalizedMetric.REVENUE: "not disclosed"},
            evidence_refs=("actual-receipt",),
        )


def test_pattern_requires_supporting_quarters_and_counterexample() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        HistoricalPattern(
            metric_family="revenue", statement="Revenue beat", supporting_fiscal_periods=(_period(2025, 1),),
            counterexample_fiscal_periods=(_period(2024, 4),),
        )
    with pytest.raises(ValidationError, match="at least 1"):
        HistoricalPattern(
            metric_family="revenue", statement="Revenue beat", supporting_fiscal_periods=(_period(2025, 1), _period(2025, 2)),
            counterexample_fiscal_periods=(),
        )


def test_metric_family_weighting_keeps_q1_priority() -> None:
    assert historical_weight("revenue", 1) > historical_weight("revenue", 2)
    assert historical_weight("revenue", 1) == Decimal("1")
    with pytest.raises(ValueError, match="Q-1"):
        historical_weight("revenue", 1, family_weights={"revenue": (Decimal("0.5"), Decimal("1"), Decimal("0.2"), Decimal("0.1"))})
