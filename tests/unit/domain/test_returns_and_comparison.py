from datetime import date
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from eventlens.domain.comparison import (
    ExpectedVsRealizedComparison,
    RealizationStatus,
    compare_expected_and_realized,
)
from eventlens.domain.events import EarningsEvent, EventTiming, resolve_event_window
from eventlens.domain.returns import PriceObservation, RealizedReturn, calculate_realized_moves
from eventlens.domain.volatility import ExpectationMeasureKind


def observation(day: int, close: str, open_: str | None = None) -> PriceObservation:
    return PriceObservation(
        trading_date=date(2026, 1, day),
        close=Decimal(close),
        open=Decimal(open_) if open_ else None,
    )


def resolved_window():
    return resolve_event_window(
        EarningsEvent(
            ticker="DEMO", event_date=date(2026, 1, 2), timing=EventTiming.AFTER_MARKET_CLOSE
        ),
        tuple(date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9)),
    )


def test_realized_returns_keep_signed_and_absolute_and_decompose_first_session() -> None:
    result = calculate_realized_moves(
        observation(2, "100"),
        (
            observation(5, "90", "95"),
            observation(6, "91"),
            observation(7, "92"),
            observation(8, "93"),
            observation(9, "94"),
        ),
        event_window=resolved_window(),
    )
    assert result.one_day is not None
    assert result.one_day.signed_return_pct == Decimal("-0.1")
    assert result.one_day.absolute_move_pct == Decimal("0.1")
    assert result.five_day is not None
    assert result.five_day.signed_return_pct == Decimal("-0.06")
    assert result.overnight_gap_pct == Decimal("-0.05")
    assert result.first_session_intraday_pct == Decimal("-0.0526315789473684210526315789")


def test_missing_first_resolved_post_event_observation_is_visible() -> None:
    result = calculate_realized_moves(
        observation(2, "100"), (), event_window=resolved_window()
    )
    assert result.one_day is None
    assert result.warnings[0].code == "first_post_event_observation_missing"
    assert result.first_post_event_trading_date == date(2026, 1, 5)


def test_missing_fifth_and_open_are_visible() -> None:
    result = calculate_realized_moves(
        observation(2, "100"), (observation(5, "101"),), event_window=resolved_window()
    )
    codes = {warning.code for warning in result.warnings}
    assert {"first_session_open_missing", "resolved_post_event_observations_missing"} <= codes


def test_missing_middle_resolved_session_blocks_five_day_instead_of_relabeling_later_price() -> None:
    result = calculate_realized_moves(
        observation(2, "100"),
        (
            observation(5, "101"),
            observation(6, "102"),
            # Jan 7 is an explicitly resolved but unavailable session.
            observation(8, "104"),
            observation(9, "105"),
        ),
        event_window=resolved_window(),
    )
    assert result.one_day is not None
    assert result.one_day.end_trading_date == date(2026, 1, 5)
    assert result.five_day is None
    assert result.fifth_post_event_trading_date == date(2026, 1, 9)
    assert result.warnings[-1].code == "resolved_post_event_observations_missing"
    assert "2026-01-07" in result.warnings[-1].message


def test_reference_must_match_resolved_window() -> None:
    with pytest.raises(ValueError, match="does not match resolved"):
        calculate_realized_moves(
            observation(5, "100"), (observation(6, "101"),), event_window=resolved_window()
        )


def test_expected_vs_realized_handles_exceeded_and_zero_or_missing_without_silent_zero() -> None:
    exceeded = compare_expected_and_realized(
        expected_move_pct=Decimal("0.05"),
        realized_move_pct=Decimal("0.10"),
        expected_measure_kind=ExpectationMeasureKind.ATM_STRADDLE_DERIVED_IMPLIED_MOVE,
    )
    assert exceeded.move_gap_pct == Decimal("0.05")
    assert exceeded.realization_ratio == Decimal("2")
    assert exceeded.status is RealizationStatus.EXCEEDED

    zero = compare_expected_and_realized(
        expected_move_pct=Decimal("0"),
        realized_move_pct=Decimal("0.10"),
        expected_measure_kind=ExpectationMeasureKind.REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY,
    )
    assert zero.realization_ratio is None
    assert zero.warnings[0].code == "zero_expected_move_ratio_undefined"

    missing = compare_expected_and_realized(
        expected_move_pct=None,
        realized_move_pct=Decimal("0.10"),
        expected_measure_kind=ExpectationMeasureKind.REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY,
    )
    assert missing.status is RealizationStatus.UNAVAILABLE
    assert missing.expected_move_pct is None


def test_proxy_measure_cannot_be_relabelled_as_implied() -> None:
    with pytest.raises(ValidationError, match="label does not match"):
        ExpectedVsRealizedComparison(
            expected_measure_kind=(
                ExpectationMeasureKind.REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY
            ),
            expected_measure_label="ATM-straddle-derived implied move",
            expected_move_pct=Decimal("0.05"),
            realized_move_pct=Decimal("0.06"),
            move_gap_pct=Decimal("0.01"),
            realization_ratio=Decimal("1.2"),
            status=RealizationStatus.EXCEEDED,
        )


def test_canonical_return_and_comparison_reject_contradictory_arithmetic() -> None:
    with pytest.raises(ValidationError, match="absolute_move_pct"):
        RealizedReturn(
            window="test",
            signed_return_pct=Decimal("-0.10"),
            absolute_move_pct=Decimal("0.01"),
            start_trading_date=date(2026, 1, 2),
            end_trading_date=date(2026, 1, 5),
        )
    with pytest.raises(ValidationError, match="ratio or status"):
        ExpectedVsRealizedComparison(
            expected_measure_kind=ExpectationMeasureKind.ATM_STRADDLE_DERIVED_IMPLIED_MOVE,
            expected_measure_label="ATM-straddle-derived implied move",
            expected_move_pct=Decimal("0.05"),
            realized_move_pct=Decimal("0.10"),
            move_gap_pct=Decimal("0.05"),
            realization_ratio=Decimal("1"),
            status=RealizationStatus.MATCHED,
        )


def test_realized_and_comparison_arithmetic_ignore_ambient_decimal_precision() -> None:
    with localcontext() as context:
        context.prec = 8
        low_precision_return = calculate_realized_moves(
            observation(2, "3"), (observation(5, "4"),), event_window=resolved_window()
        ).one_day
        low_precision_comparison = compare_expected_and_realized(
            expected_move_pct=Decimal("0.03"),
            realized_move_pct=Decimal("0.01"),
            expected_measure_kind=ExpectationMeasureKind.ATM_STRADDLE_DERIVED_IMPLIED_MOVE,
        )
    with localcontext() as context:
        context.prec = 50
        high_precision_return = calculate_realized_moves(
            observation(2, "3"), (observation(5, "4"),), event_window=resolved_window()
        ).one_day
        high_precision_comparison = compare_expected_and_realized(
            expected_move_pct=Decimal("0.03"),
            realized_move_pct=Decimal("0.01"),
            expected_measure_kind=ExpectationMeasureKind.ATM_STRADDLE_DERIVED_IMPLIED_MOVE,
        )

    assert low_precision_return == high_precision_return
    assert low_precision_comparison == high_precision_comparison
