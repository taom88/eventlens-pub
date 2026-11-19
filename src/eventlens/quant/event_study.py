"""Deterministic event-conditioned validation and statistical utilities."""

from __future__ import annotations

from decimal import Decimal, localcontext
from random import Random

from .models import (
    AlphaZooAssessment,
    BetaRegressionResult,
    BootstrapConfidenceInterval,
    EventCohortObservation,
    Market,
    MarketAssumptions,
    TransactionCostResult,
    HK_MARKET_ASSUMPTIONS,
    US_MARKET_ASSUMPTIONS,
)


def market_assumptions(market: Market) -> MarketAssumptions:
    return US_MARKET_ASSUMPTIONS if market is Market.US else HK_MARKET_ASSUMPTIONS


def validate_event_cohort(
    observations: tuple[EventCohortObservation, ...], *, minimum_events: int = 5
) -> tuple[EventCohortObservation, ...]:
    """Validate a point-in-time cohort without silently dropping missing rows.

    The returned value preserves unavailable observations for audit.  The
    minimum is applied to ``is_comparable`` rows only, so a sparse cohort
    cannot look backtest-ready merely because missing events were omitted.
    """

    if minimum_events < 2:
        raise ValueError("minimum_events must be at least two")
    event_ids = [observation.event_id for observation in observations]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("event cohort contains duplicate event IDs")
    comparable = sum(observation.is_comparable for observation in observations)
    if comparable < minimum_events:
        raise ValueError(
            "insufficient comparable events for an event-conditioned cohort "
            f"({comparable} comparable of {len(observations)} observations)"
        )
    markets = {observation.market for observation in observations}
    if len(markets) != 1:
        raise ValueError("event cohort cannot combine markets with different exchange calendars")
    return tuple(sorted(observations, key=lambda item: (item.event_date, item.event_id)))


def apply_transaction_costs(
    gross_return_pct: Decimal, turnover: Decimal, assumptions: MarketAssumptions
) -> TransactionCostResult:
    """Apply cost to each one-way notional leg in conventional turnover.

    ``turnover`` is the sum of absolute portfolio-weight changes, not a
    normalized [0, 1] allocation.  A full entry followed by a full exit is
    therefore turnover ``2`` and incurs two one-way cost legs.
    """

    if turnover < 0:
        raise ValueError("turnover cannot be negative")
    with localcontext() as context:
        context.prec = 28
        one_way = (
            assumptions.commission_bps
            + assumptions.fees_bps
            + (assumptions.bid_ask_spread_bps / Decimal("2"))
            + assumptions.slippage_bps
        )
        total_cost = turnover * one_way / Decimal("10000")
        net = gross_return_pct - total_cost
    return TransactionCostResult(
        gross_return_pct=gross_return_pct,
        turnover=turnover,
        entry_exit_legs=int(turnover.to_integral_value(rounding="ROUND_CEILING")),
        one_way_cost_bps=one_way,
        total_cost_pct=total_cost,
        net_return_pct=net,
        market_impact_caveat=assumptions.market_impact_caveat,
    )


def bootstrap_mean_confidence_interval(
    values: tuple[Decimal, ...], *, seed: int, resamples: int = 2_000,
    confidence_level: Decimal = Decimal("0.95"),
    missing_observation_count: int = 0,
    sensitivity_resamples: tuple[int, ...] = (),
) -> BootstrapConfidenceInterval:
    """Fixed-seed percentile bootstrap for a small event cohort."""

    if len(values) < 2:
        raise ValueError("bootstrap requires at least two observations")
    if resamples < 100:
        raise ValueError("bootstrap resamples must be at least 100")
    if not Decimal("0") < confidence_level < Decimal("1"):
        raise ValueError("confidence_level must be between zero and one")
    if missing_observation_count < 0:
        raise ValueError("missing_observation_count cannot be negative")
    if any(item < 100 for item in sensitivity_resamples):
        raise ValueError("sensitivity resamples must be at least 100")
    if len(set(sensitivity_resamples)) != len(sensitivity_resamples):
        raise ValueError("sensitivity resamples must be unique")
    with localcontext() as context:
        context.prec = 28
        estimate = sum(values, Decimal("0")) / Decimal(len(values))
        lower, upper = _bootstrap_interval(values, seed, resamples, confidence_level, estimate)
        sensitivity = tuple(
            _bootstrap_sensitivity(values, seed, item, confidence_level, estimate)
            for item in sensitivity_resamples
        )
    return BootstrapConfidenceInterval(
        seed=seed, resamples=resamples, sample_count=len(values), estimate_pct=estimate,
        lower_pct=lower, upper_pct=upper, confidence_level=confidence_level,
        missing_observation_count=missing_observation_count,
        sensitivity=sensitivity,
    )


def beta_regression(
    security_returns_pct: tuple[Decimal | None, ...],
    benchmark_returns_pct: tuple[Decimal | None, ...],
    *,
    benchmark_id: str,
    benchmark_market: Market = Market.US,
    benchmark_exchange_calendar: str = "XNYS",
    minimum_samples: int = 3,
) -> BetaRegressionResult:
    """OLS alpha/beta with visible edge-case output, never fabricated coefficients."""

    if len(security_returns_pct) != len(benchmark_returns_pct):
        raise ValueError("security and benchmark samples must have equal length")
    if minimum_samples < 3:
        raise ValueError("minimum_samples must be at least three")
    assumptions = market_assumptions(benchmark_market)
    if benchmark_exchange_calendar != assumptions.exchange_calendar:
        raise ValueError("benchmark exchange calendar does not match benchmark market")
    pairs = tuple(
        (security, benchmark)
        for security, benchmark in zip(security_returns_pct, benchmark_returns_pct)
        if security is not None and benchmark is not None
    )
    count = len(pairs)
    missing = len(security_returns_pct) - count
    common = {
        "sample_count": count,
        "input_pair_count": len(security_returns_pct),
        "missing_pair_count": missing,
        "minimum_sample_count": minimum_samples,
        "benchmark_id": benchmark_id,
        "benchmark_market": benchmark_market,
        "benchmark_exchange_calendar": benchmark_exchange_calendar,
    }
    if count < minimum_samples:
        return BetaRegressionResult(**common, warning="insufficient paired observations for beta regression")
    security_values = tuple(pair[0] for pair in pairs)
    benchmark_values = tuple(pair[1] for pair in pairs)
    with localcontext() as context:
        context.prec = 28
        mean_x = sum(benchmark_values, Decimal("0")) / Decimal(count)
        mean_y = sum(security_values, Decimal("0")) / Decimal(count)
        variance_x = sum((item - mean_x) ** 2 for item in benchmark_values)
        if variance_x == 0:
            return BetaRegressionResult(**common, warning="benchmark variance is zero")
        covariance = sum(
            (x - mean_x) * (y - mean_y)
            for x, y in zip(benchmark_values, security_values)
        )
        beta = covariance / variance_x
        alpha = mean_y - (beta * mean_x)
        fitted = tuple(alpha + beta * value for value in benchmark_values)
        total = sum((item - mean_y) ** 2 for item in security_values)
        residual = sum((actual - expected) ** 2 for actual, expected in zip(security_values, fitted))
        r_squared = Decimal("0") if total == 0 else max(Decimal("0"), Decimal("1") - (residual / total))
    return BetaRegressionResult(
        **common,
        alpha_pct=alpha,
        beta=beta,
        r_squared=r_squared,
        warning="security variance is zero" if total == 0 else None,
    )


def alpha_zoo_assessment(candidate_signal_id: str, tested_candidates: int, raw_p_value: Decimal | None) -> AlphaZooAssessment:
    """Bonferroni adjustment plus a permanent exploratory-search warning."""

    if tested_candidates < 1:
        raise ValueError("tested_candidates must be positive")
    adjusted = None if raw_p_value is None else min(Decimal("1"), raw_p_value * tested_candidates)
    return AlphaZooAssessment(
        candidate_signal_id=candidate_signal_id,
        tested_candidates=tested_candidates,
        raw_p_value=raw_p_value,
        adjusted_p_value=adjusted,
        upstream_integration_status="blocked_unverified_upstream_api",
        exploratory_warning=(
            "Candidate signal only: Alpha Zoo search surfaces create multiple-testing risk; "
            "out-of-sample validation is required."
        ),
    )


def _bootstrap_interval(
    values: tuple[Decimal, ...], seed: int, resamples: int,
    confidence_level: Decimal, estimate: Decimal,
) -> tuple[Decimal, Decimal]:
    random = Random(seed)
    estimates = sorted(
        sum((values[random.randrange(len(values))] for _ in values), Decimal("0"))
        / Decimal(len(values))
        for _ in range(resamples)
    )
    alpha = (Decimal("1") - confidence_level) / Decimal("2")
    lower_index = int((alpha * Decimal(resamples - 1)).to_integral_value(rounding="ROUND_FLOOR"))
    upper_index = int(((Decimal("1") - alpha) * Decimal(resamples - 1)).to_integral_value(rounding="ROUND_CEILING"))
    return min(estimates[lower_index], estimate), max(estimates[upper_index], estimate)


def _bootstrap_sensitivity(values, seed, resamples, confidence_level, estimate):
    from .models import BootstrapSensitivity

    lower, upper = _bootstrap_interval(values, seed, resamples, confidence_level, estimate)
    return BootstrapSensitivity(resamples=resamples, lower_pct=lower, upper_pct=upper)
