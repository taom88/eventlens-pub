from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from eventlens.providers.fixture import FixtureProvider
from eventlens.quant.backtest import run_event_study
from eventlens.quant.models import US_MARKET_ASSUMPTIONS, Market
from eventlens.quant.supplement import (
    BenchmarkEventObservation,
    CostScenarioPolicy,
    build_event_study_supplement,
)


def _report():
    provider = FixtureProvider(Path("fixtures"))
    events = provider.get_earnings_events("DEMO", date(2020, 1, 1), date(2030, 1, 1)).data.events
    history = provider.get_daily_history("DEMO", date(2020, 1, 1), date(2030, 1, 1)).data
    return run_event_study("DEMO", events, history)


def _policy():
    return CostScenarioPolicy(
        market_assumptions=US_MARKET_ASSUMPTIONS,
        turnover=Decimal("2"),
        policy_version="us-round-trip-v1",
    )


def test_supplement_pairs_by_event_id_and_requires_four_beta_observations() -> None:
    report = _report()
    benchmark = tuple(
        BenchmarkEventObservation(
            event_id=item.event_id,
            event_date=item.event_date,
            t_plus_1_return_pct=Decimal("0.01"),
            t_plus_5_return_pct=Decimal("0.02"),
        )
        for item in reversed(report.observations)
    )
    supplement = build_event_study_supplement(
        report, benchmark, benchmark_id="QQQ", benchmark_market=Market.US,
        cost_policy=_policy(),
    )
    assert supplement.execution_status == "analysis_only"
    assert supplement.benchmark.paired_t_plus_1_count == 10
    assert supplement.benchmark.t_plus_1_beta.sample_count == 10
    assert supplement.base_report_sha256
    assert supplement.t_plus_1_costs.gross_return_pct != supplement.t_plus_1_costs.net_return_pct


def test_supplement_rejects_duplicate_or_mismatched_benchmark_events() -> None:
    report = _report()
    item = report.observations[0]
    benchmark = BenchmarkEventObservation(event_id=item.event_id, event_date=item.event_date)
    with pytest.raises(ValueError, match="unique event IDs"):
        build_event_study_supplement(
            report, (benchmark, benchmark), benchmark_id="SPY", benchmark_market=Market.US,
            cost_policy=_policy(),
        )
    with pytest.raises(ValueError, match="date mismatch"):
        build_event_study_supplement(
            report,
            (benchmark.model_copy(update={"event_date": date(2030, 1, 1)}),),
            benchmark_id="SPY", benchmark_market=Market.US, cost_policy=_policy(),
        )


def test_supplement_is_deterministic_and_missing_pairs_are_visible() -> None:
    report = _report()
    benchmark = tuple(
        BenchmarkEventObservation(
            event_id=item.event_id, event_date=item.event_date,
            t_plus_1_return_pct=Decimal("0.01"),
            t_plus_5_return_pct=Decimal("0.02"),
        )
        for item in report.observations[:4]
    )
    first = build_event_study_supplement(
        report, benchmark, benchmark_id="QQQ", benchmark_market=Market.US, cost_policy=_policy()
    )
    second = build_event_study_supplement(
        report, tuple(reversed(benchmark)), benchmark_id="QQQ", benchmark_market=Market.US, cost_policy=_policy()
    )
    assert first == second
    assert first.benchmark.missing_pair_count == 6
    assert "fewer than four" not in " ".join(first.warnings)
