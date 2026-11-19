"""Deterministic v4 factual market-reaction windows."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    REACTION_METHODOLOGY_VERSION,
    ReactionReturnSet,
    benchmark_adjust,
    calculate_market_reaction,
)
from eventlens.domain.events import EarningsEvent, EventTiming, resolve_event_window
from eventlens.domain.returns import PriceObservation, calculate_realized_moves


def observation(day: int, close: str, open_: str | None = None) -> PriceObservation:
    return PriceObservation(
        trading_date=date(2026, 1, day),
        close=Decimal(close),
        open=Decimal(open_) if open_ is not None else None,
    )


def amc_window():
    return resolve_event_window(
        EarningsEvent(
            ticker="DEMO", event_date=date(2026, 1, 2), timing=EventTiming.AFTER_MARKET_CLOSE
        ),
        tuple(date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9)),
    )


def post_event_prices() -> tuple[PriceObservation, ...]:
    return (
        observation(5, "110"),
        observation(6, "120"),
        observation(7, "130"),
        observation(8, "140"),
        observation(9, "150"),
    )


def test_bmo_and_amc_use_the_resolved_session_convention() -> None:
    dates = tuple(date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9))
    amc = amc_window()
    bmo = resolve_event_window(
        EarningsEvent(
            ticker="DEMO", event_date=date(2026, 1, 5), timing=EventTiming.BEFORE_MARKET_OPEN
        ),
        dates,
    )

    assert (amc.reference_trading_date, amc.first_post_event_trading_date) == (
        date(2026, 1, 2),
        date(2026, 1, 5),
    )
    assert (bmo.reference_trading_date, bmo.first_post_event_trading_date) == (
        date(2026, 1, 2),
        date(2026, 1, 5),
    )

    reaction = calculate_market_reaction(
        observation(2, "100"), post_event_prices(), event_window=bmo, fiscal_period="FY2026_Q2"
    )
    assert reaction.prices.t_plus_1 is not None
    assert reaction.prices.t_plus_1.trading_date == date(2026, 1, 5)


def test_t_plus_windows_are_exact_and_t_plus_two_is_not_t_plus_five() -> None:
    reaction = calculate_market_reaction(
        observation(2, "100"),
        post_event_prices(),
        event_window=amc_window(),
        fiscal_period="FY2026_Q2",
        after_hours_price=Decimal("105"),
    )

    assert reaction.returns.after_hours_pct == Decimal("0.05")
    assert reaction.returns.t_plus_1_pct == Decimal("0.1")
    assert reaction.returns.t_plus_2_pct == Decimal("0.2")
    assert reaction.returns.t_plus_5_pct == Decimal("0.5")
    assert reaction.prices.t_plus_2 is not None
    assert reaction.prices.t_plus_2.trading_date == date(2026, 1, 6)
    assert reaction.prices.t_plus_5 is not None
    assert reaction.prices.t_plus_5.trading_date == date(2026, 1, 9)
    assert reaction.methodology_version == REACTION_METHODOLOGY_VERSION


def test_missing_after_hours_remains_null_and_is_visible() -> None:
    reaction = calculate_market_reaction(
        observation(2, "100"), post_event_prices(), event_window=amc_window(), fiscal_period="FY2026_Q2"
    )

    assert reaction.prices.after_hours is None
    assert reaction.returns.after_hours_pct is None
    assert "after_hours_price_missing" in {warning.code for warning in reaction.warnings}


def test_next_open_and_first_session_decomposition_are_explicit_and_exact() -> None:
    reaction = calculate_market_reaction(
        observation(2, "100"),
        (
            observation(5, "110", "105"),
            observation(6, "120"),
            observation(7, "130"),
            observation(8, "140"),
            observation(9, "150"),
        ),
        event_window=amc_window(),
        fiscal_period="FY2026_Q2",
    )

    assert reaction.prices.next_open == Decimal("105")
    assert reaction.returns.next_session_open_vs_reference_close_pct == Decimal("0.05")
    assert reaction.returns.overnight_gap_pct == Decimal("0.05")
    assert reaction.returns.first_session_intraday_pct == Decimal(
        "0.047619047619047619047619048"
    )


def test_context_card_reaction_matches_canonical_domain_returns() -> None:
    reference = observation(2, "100")
    sessions = post_event_prices()
    reaction = calculate_market_reaction(
        reference, sessions, event_window=amc_window(), fiscal_period="FY2026_Q2"
    )
    canonical = calculate_realized_moves(reference, sessions, event_window=amc_window())
    assert reaction.returns.t_plus_1_pct == canonical.one_day.signed_return_pct
    assert reaction.returns.t_plus_5_pct == canonical.five_day.signed_return_pct
    assert reaction.returns.overnight_gap_pct == canonical.overnight_gap_pct
    assert reaction.returns.first_session_intraday_pct == canonical.first_session_intraday_pct


def test_missing_first_session_open_keeps_decomposition_null_with_warning() -> None:
    reaction = calculate_market_reaction(
        observation(2, "100"), post_event_prices(), event_window=amc_window(), fiscal_period="FY2026_Q2"
    )

    assert reaction.prices.next_open is None
    assert reaction.returns.next_session_open_vs_reference_close_pct is None
    assert reaction.returns.overnight_gap_pct is None
    assert reaction.returns.first_session_intraday_pct is None
    assert "first_session_open_missing" in {warning.code for warning in reaction.warnings}


def test_benchmark_adjustment_is_security_minus_selected_benchmark() -> None:
    reaction = calculate_market_reaction(
        observation(2, "100"),
        (
            observation(5, "110", "105"),
            observation(6, "120"),
            observation(7, "130"),
            observation(8, "140"),
            observation(9, "150"),
        ),
        event_window=amc_window(),
        fiscal_period="FY2026_Q2",
        after_hours_price=Decimal("105"),
        benchmark_reference=observation(2, "200"),
        benchmark_post_event_sessions=(
            observation(5, "220", "210"),
            observation(6, "230"),
            observation(7, "240"),
            observation(8, "245"),
            observation(9, "250"),
        ),
        benchmark_after_hours_price=Decimal("210"),
        benchmark_ticker="BENCH",
    )

    assert reaction.benchmark is not None
    assert reaction.benchmark.ticker == "BENCH"
    assert reaction.benchmark_adjusted_returns == ReactionReturnSet(
        after_hours_pct=Decimal("0"),
        t_plus_1_pct=Decimal("0"),
        t_plus_2_pct=Decimal("0.05"),
        t_plus_5_pct=Decimal("0.25"),
    )
    assert reaction.returns.overnight_gap_pct == Decimal("0.05")
    assert reaction.benchmark.returns.overnight_gap_pct == Decimal("0.05")
    # The open-to-close decomposition is raw context, not a relative-return horizon.
    assert reaction.benchmark_adjusted_returns.next_session_open_vs_reference_close_pct is None
    assert reaction.benchmark_adjusted_returns.overnight_gap_pct is None
    assert reaction.benchmark_adjusted_returns.first_session_intraday_pct is None

    from eventlens.context_cards import MarketReaction

    payload = reaction.model_dump()
    payload["benchmark_adjusted_returns"]["next_session_open_vs_reference_close_pct"] = Decimal("0")  # type: ignore[index]
    with pytest.raises(ValidationError, match="cannot include first-session open decomposition"):
        MarketReaction.model_validate(payload)


def test_absent_intermediate_sessions_block_the_windows_that_require_them() -> None:
    reaction = calculate_market_reaction(
        observation(2, "100"),
        (
            observation(5, "110"),
            # Jan. 6 is resolved but unavailable.
            observation(7, "130"),
            observation(8, "140"),
            observation(9, "150"),
        ),
        event_window=amc_window(),
        fiscal_period="FY2026_Q2",
    )

    assert reaction.returns.t_plus_1_pct == Decimal("0.1")
    assert reaction.returns.t_plus_2_pct is None
    assert reaction.returns.t_plus_5_pct is None
    codes = {warning.code for warning in reaction.warnings}
    assert {"t_plus_2_observations_missing", "t_plus_5_observations_missing"} <= codes


def test_relative_returns_and_calculation_ignore_ambient_decimal_precision() -> None:
    security = ReactionReturnSet(t_plus_1_pct=Decimal("0.3333333333333333333333333333"))
    benchmark = ReactionReturnSet(t_plus_1_pct=Decimal("0.1428571428571428571428571429"))
    with localcontext() as context:
        context.prec = 5
        low_precision = benchmark_adjust(security, benchmark)
    with localcontext() as context:
        context.prec = 50
        high_precision = benchmark_adjust(security, benchmark)

    assert low_precision == high_precision

    with localcontext() as context:
        context.prec = 5
        low_precision_reaction = calculate_market_reaction(
            observation(2, "3"),
            (observation(5, "4"),),
            event_window=amc_window(),
            fiscal_period="FY2026_Q2",
        )
    with localcontext() as context:
        context.prec = 50
        high_precision_reaction = calculate_market_reaction(
            observation(2, "3"),
            (observation(5, "4"),),
            event_window=amc_window(),
            fiscal_period="FY2026_Q2",
        )

    assert low_precision_reaction == high_precision_reaction
    assert low_precision_reaction.returns.t_plus_1_pct == Decimal(
        "0.333333333333333333333333333"
    )


def test_partial_benchmark_inputs_and_orphan_adjustment_are_rejected() -> None:
    with pytest.raises(ValueError, match="supplied together"):
        calculate_market_reaction(
            observation(2, "100"),
            post_event_prices(),
            event_window=amc_window(),
            fiscal_period="FY2026_Q2",
            benchmark_reference=observation(2, "200"),
        )

    with pytest.raises(ValidationError, match="requires benchmark"):
        from eventlens.context_cards import MarketReaction

        MarketReaction(
            fiscal_period="FY2026_Q2",
            event_window=amc_window(),
            prices={"reference": observation(2, "100")},
            returns={},
            benchmark_adjusted_returns={},
        )


def test_serialized_endpoints_cannot_be_relabelled_as_different_event_sessions() -> None:
    from eventlens.context_cards import MarketReaction

    with pytest.raises(ValidationError, match="t_plus_1 price does not match"):
        MarketReaction(
            fiscal_period="FY2026_Q2",
            event_window=amc_window(),
            prices={
                "reference": observation(2, "100"),
                # Jan. 6 is T+2, never T+1 for this resolved event window.
                "t_plus_1": observation(6, "110"),
            },
            returns={},
        )


def test_canonical_next_open_return_rejects_serialized_tampering() -> None:
    from eventlens.context_cards import MarketReaction

    reaction = calculate_market_reaction(
        observation(2, "100"),
        (observation(5, "110", "105"),),
        event_window=amc_window(),
        fiscal_period="FY2026_Q2",
    )
    payload = reaction.model_dump()
    payload["returns"]["next_session_open_vs_reference_close_pct"] = Decimal("0.06")  # type: ignore[index]
    with pytest.raises(ValidationError, match="canonical next-open return"):
        MarketReaction.model_validate(payload)

    payload = reaction.model_dump()
    payload["prices"]["next_open"] = Decimal("106")  # type: ignore[index]
    with pytest.raises(ValidationError, match="next_open must match"):
        MarketReaction.model_validate(payload)
