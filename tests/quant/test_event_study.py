from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from eventlens.quant.event_study import (
    alpha_zoo_assessment,
    apply_transaction_costs,
    beta_regression,
    bootstrap_mean_confidence_interval,
    market_assumptions,
    validate_event_cohort,
)
from eventlens.quant.models import EventCohortObservation, Market, ScenarioAsset
from eventlens.quant.optimization import optimize_worst_case_scenario


def test_transaction_costs_and_market_conventions_are_explicit() -> None:
    result = apply_transaction_costs(Decimal("0.05"), Decimal("2"), market_assumptions(Market.HONG_KONG))
    assert result.total_cost_pct > 0
    assert result.entry_exit_legs == 2
    assert "entry_and_exit" in result.turnover_convention
    assert market_assumptions(Market.US).currency == "USD"
    assert market_assumptions(Market.US).exchange_calendar == "XNYS"
    assert market_assumptions(Market.HONG_KONG).exchange_calendar == "XHKG"


def test_cohort_rejects_post_cutoff_leakage_and_duplicate_events() -> None:
    cutoff = datetime(2026, 7, 25, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="post-cutoff"):
        EventCohortObservation(event_id="e1", event_date=date(2026, 7, 1), feature_available_at=datetime(2026, 7, 26, tzinfo=timezone.utc), information_cutoff_at=cutoff, realized_return_pct=Decimal("0.01"))
    cohort = tuple(EventCohortObservation(event_id=f"e{i}", event_date=date(2025, i, 1), feature_available_at=cutoff, information_cutoff_at=cutoff, realized_return_pct=Decimal("0.01")) for i in range(1, 6))
    assert len(validate_event_cohort(cohort)) == 5
    with pytest.raises(ValueError, match="duplicate"):
        validate_event_cohort(cohort + (cohort[0],), minimum_events=5)
    missing = cohort[:-1] + (
        EventCohortObservation(
            event_id="missing", event_date=date(2025, 6, 1), feature_available_at=None,
            information_cutoff_at=cutoff, realized_return_pct=None,
            missing_observation_reason="delisted before outcome session",
        ),
    )
    with pytest.raises(ValueError, match="4 comparable"):
        validate_event_cohort(missing)
    with pytest.raises(ValueError, match="exchange calendar"):
        EventCohortObservation(
            event_id="hk", event_date=date(2025, 6, 1), market=Market.HONG_KONG,
            exchange_calendar="XNYS", feature_available_at=cutoff,
            information_cutoff_at=cutoff, realized_return_pct=Decimal("0"),
        )


def test_bootstrap_is_seed_reproducible_and_beta_handles_edge_cases() -> None:
    values = (Decimal("-0.02"), Decimal("0.01"), Decimal("0.03"), Decimal("0.04"))
    first = bootstrap_mean_confidence_interval(
        values, seed=7, missing_observation_count=2, sensitivity_resamples=(100, 500)
    )
    assert first == bootstrap_mean_confidence_interval(
        values, seed=7, missing_observation_count=2, sensitivity_resamples=(100, 500)
    )
    assert first.missing_observation_count == 2
    assert [item.resamples for item in first.sensitivity] == [100, 500]
    assert beta_regression((Decimal(".01"),), (Decimal(".02"),), benchmark_id="BENCH").warning
    assert beta_regression((Decimal(".01"), Decimal(".02"), Decimal(".03")), (Decimal(".01"), Decimal(".01"), Decimal(".01")), benchmark_id="BENCH").warning == "benchmark variance is zero"
    assert beta_regression(
        (Decimal(".01"), Decimal(".01"), Decimal(".01")),
        (Decimal(".01"), Decimal(".02"), Decimal(".03")),
        benchmark_id="BENCH",
    ).warning == "security variance is zero"
    beta = beta_regression(
        (Decimal(".01"), None, Decimal(".03"), Decimal(".04")),
        (Decimal(".02"), Decimal(".01"), Decimal(".02"), Decimal(".03")),
        benchmark_id="SPY",
    )
    assert beta.missing_pair_count == 1
    assert beta.benchmark_exchange_calendar == "XNYS"


def test_scenario_optimizer_respects_constraints_and_alpha_zoo_warns() -> None:
    assets = (
        ScenarioAsset(asset_id="A", scenario_returns_pct=(Decimal(".01"), Decimal("-.01")), current_weight=Decimal("0"), max_weight=Decimal(".6"), liquidity_limit=Decimal(".6")),
        ScenarioAsset(asset_id="B", scenario_returns_pct=(Decimal(".02"), Decimal(".00")), current_weight=Decimal("0"), max_weight=Decimal(".6"), liquidity_limit=Decimal(".6")),
    )
    result = optimize_worst_case_scenario(assets, concentration_limit=Decimal(".5"), max_turnover=Decimal(".75"))
    assert result.turnover <= Decimal(".75")
    assert all(weight <= Decimal(".5") for _, weight in result.weights)
    assert sum(weight for _, weight in result.weights) + result.cash_weight == Decimal("1")
    assert result.objective.endswith("not_global_maximin")
    alpha = alpha_zoo_assessment("candidate", 20, Decimal(".01"))
    assert alpha.adjusted_p_value == Decimal(".20")
    assert "multiple-testing" in alpha.exploratory_warning
    assert alpha.upstream_integration_status == "blocked_unverified_upstream_api"


def test_greedy_optimizer_is_not_claimed_to_be_global_maximin() -> None:
    """Reviewer counterexample: diversification beats the documented heuristic."""

    assets = (
        ScenarioAsset(asset_id="A", scenario_returns_pct=(Decimal(".10"), Decimal("-.10")), current_weight=Decimal("0"), max_weight=Decimal(".5"), liquidity_limit=Decimal(".5")),
        ScenarioAsset(asset_id="B", scenario_returns_pct=(Decimal("-.10"), Decimal(".10")), current_weight=Decimal("0"), max_weight=Decimal(".5"), liquidity_limit=Decimal(".5")),
        ScenarioAsset(asset_id="C", scenario_returns_pct=(Decimal("0"), Decimal("0")), current_weight=Decimal("0"), max_weight=Decimal(".5"), liquidity_limit=Decimal(".5")),
    )
    result = optimize_worst_case_scenario(assets, concentration_limit=Decimal(".5"), max_turnover=Decimal("1"))
    # A=50%, B=50%, C=0% is feasible and has a 0% worst case; the greedy
    # ranker chooses C first and has a negative worst case.  This guards the
    # public result label against a false global-optimum claim.
    assert result.expected_worst_case_return_pct < Decimal("0")
    assert "heuristic_not_global_maximin" in result.objective
