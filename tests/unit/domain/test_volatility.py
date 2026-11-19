from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from eventlens.domain.events import EarningsEvent, EventTiming, resolve_event_window
from eventlens.domain.volatility import (
    ATMStraddleImpliedMove,
    DailyClose,
    OptionChainSnapshot,
    OptionQuote,
    OptionSelectionError,
    OptionSnapshotKind,
    OptionType,
    PreEventSnapshotAlignment,
    calculate_atm_straddle_implied_move,
    calculate_realized_volatility_expected_move_proxy,
    select_atm_straddle,
    select_last_pre_event_snapshot,
)


EVENT = date(2026, 1, 6)
EXPIRY = date(2026, 1, 9)
AS_OF = datetime(2026, 1, 6, 15, 59, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc)
TRADING_DATES = tuple(date(2026, 1, day) for day in (5, 6, 7, 8, 9, 12, 13))


def window(timing: EventTiming = EventTiming.AFTER_MARKET_CLOSE):
    return resolve_event_window(
        EarningsEvent(ticker="DEMO", event_date=EVENT, timing=timing),
        TRADING_DATES,
    )


def quote(kind: OptionType, strike: str, bid: str, ask: str, expiry: date = EXPIRY) -> OptionQuote:
    return OptionQuote(
        option_type=kind,
        strike=Decimal(strike),
        expiration=expiry,
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


def chain(*quotes: OptionQuote) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        ticker="DEMO",
        snapshot_date=EVENT,
        as_of=AS_OF,
        snapshot_kind=OptionSnapshotKind.END_OF_DAY,
        pre_event_alignment=PreEventSnapshotAlignment.VERIFIED_PRE_EVENT,
        spot=Decimal("100"),
        source="fixture_options",
        quotes=quotes,
    )


def test_atm_selection_uses_nearest_eligible_expiry_then_lower_strike_tie_break() -> None:
    snapshot = chain(
        quote(OptionType.CALL, "95", "3", "3.2"),
        quote(OptionType.PUT, "95", "2", "2.2"),
        quote(OptionType.CALL, "105", "2", "2.2"),
        quote(OptionType.PUT, "105", "3", "3.2"),
        quote(OptionType.CALL, "100", "8", "8.2", expiry=date(2026, 1, 16)),
        quote(OptionType.PUT, "100", "8", "8.2", expiry=date(2026, 1, 16)),
    )

    selection = select_atm_straddle(
        snapshot,
        event_window=window(),
        information_cutoff_at=CUTOFF,
    )
    assert selection.expiration == EXPIRY
    assert selection.strike == Decimal("95")


def test_eligible_expiration_can_require_first_post_event_session_for_amc() -> None:
    same_day = EVENT
    snapshot = chain(
        quote(OptionType.CALL, "100", "2", "2.2", expiry=same_day),
        quote(OptionType.PUT, "100", "2", "2.2", expiry=same_day),
        quote(OptionType.CALL, "100", "5", "5.2"),
        quote(OptionType.PUT, "100", "5", "5.2"),
    )

    result = calculate_atm_straddle_implied_move(
        snapshot,
        event_window=window(),
        information_cutoff_at=CUTOFF,
    )
    assert result.expiration == EXPIRY
    assert result.expected_move_pct == Decimal("0.102")


def test_same_day_expiry_is_rejected_for_after_market_close_event() -> None:
    snapshot = chain(
        quote(OptionType.CALL, "100", "2", "2.2", expiry=EVENT),
        quote(OptionType.PUT, "100", "2", "2.2", expiry=EVENT),
    )
    with pytest.raises(OptionSelectionError, match="no option expiration"):
        calculate_atm_straddle_implied_move(
            snapshot,
            event_window=window(),
            information_cutoff_at=CUTOFF,
        )


def test_same_day_expiry_is_eligible_for_before_market_open_event() -> None:
    snapshot = chain(
        quote(OptionType.CALL, "100", "2", "2.2", expiry=EVENT),
        quote(OptionType.PUT, "100", "2", "2.2", expiry=EVENT),
    ).model_copy(
        update={"as_of": datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)}
    )

    result = calculate_atm_straddle_implied_move(
        snapshot,
        event_window=window(EventTiming.BEFORE_MARKET_OPEN),
        information_cutoff_at=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
    )
    assert result.expiration == EVENT


def test_stale_snapshot_is_labeled_as_an_approximation() -> None:
    snapshot = OptionChainSnapshot(
        ticker="DEMO",
        snapshot_date=date(2026, 1, 5),
        as_of=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        snapshot_kind=OptionSnapshotKind.END_OF_DAY,
        pre_event_alignment=PreEventSnapshotAlignment.APPROXIMATE_PRE_EVENT,
        spot=Decimal("100"),
        source="fixture_options",
        quotes=(
            quote(OptionType.CALL, "100", "2", "2.2"),
            quote(OptionType.PUT, "100", "2", "2.2"),
        ),
    )
    result = calculate_atm_straddle_implied_move(
        snapshot,
        event_window=window(),
        information_cutoff_at=CUTOFF,
    )
    assert "reference trading date" in (result.approximation_note or "")
    assert "alignment is not verified" in (result.approximation_note or "")


def test_same_date_snapshot_before_desired_cutoff_is_labeled_approximate() -> None:
    snapshot = chain(
        quote(OptionType.CALL, "100", "2", "2.2"),
        quote(OptionType.PUT, "100", "2", "2.2"),
    )

    result = calculate_atm_straddle_implied_move(
        snapshot,
        event_window=window(),
        information_cutoff_at=CUTOFF,
    )

    assert result.snapshot_lag_seconds == Decimal("18060.0")
    assert "desired information cutoff" in (result.approximation_note or "")

    with pytest.raises(ValidationError, match="arithmetically inconsistent"):
        ATMStraddleImpliedMove.model_validate(
            result.model_dump() | {"call_mid": Decimal("99")}
        )


@pytest.mark.parametrize(
    ("bid", "ask"),
    [("-0.01", "1"), ("1", "-0.01"), ("2", "1"), ("NaN", "1"), ("Infinity", "1")],
)
def test_invalid_or_crossed_quotes_fail_validation(bid: str, ask: str) -> None:
    with pytest.raises(ValidationError):
        quote(OptionType.CALL, "100", bid, ask)


def test_chain_without_complete_pair_fails_visibly() -> None:
    snapshot = chain(quote(OptionType.CALL, "100", "2", "2.2"))
    with pytest.raises(OptionSelectionError, match="complete call/put pair"):
        select_atm_straddle(
            snapshot,
            event_window=window(),
            information_cutoff_at=CUTOFF,
        )


def test_invalid_option_path_does_not_auto_create_a_proxy() -> None:
    snapshot = chain(quote(OptionType.CALL, "100", "2", "2.2"))
    with pytest.raises(OptionSelectionError):
        calculate_atm_straddle_implied_move(
            snapshot,
            event_window=window(),
            information_cutoff_at=CUTOFF,
        )
    proxy = calculate_realized_volatility_expected_move_proxy(
        (
            DailyClose(trading_date=date(2026, 1, 2), adjusted_close=Decimal("100")),
            DailyClose(trading_date=date(2026, 1, 5), adjusted_close=Decimal("101")),
            DailyClose(trading_date=date(2026, 1, 6), adjusted_close=Decimal("99")),
        ),
        event_horizon_trading_days=4,
        source="fixture_prices",
    )
    assert proxy.calculation_method.value == "realized_volatility_expected_move_proxy"


def test_duplicate_contract_quote_fails_visibly_instead_of_depending_on_input_order() -> None:
    with pytest.raises(ValidationError, match="duplicate option contracts"):
        chain(
            quote(OptionType.CALL, "100", "2", "2.2"),
            quote(OptionType.CALL, "100", "2.1", "2.3"),
            quote(OptionType.PUT, "100", "2", "2.2"),
        )


def test_realized_vol_proxy_is_labeled_and_uses_sample_stddev() -> None:
    closes = (
        DailyClose(trading_date=date(2026, 1, 2), adjusted_close=Decimal("100")),
        DailyClose(trading_date=date(2026, 1, 5), adjusted_close=Decimal("200")),
        DailyClose(trading_date=date(2026, 1, 6), adjusted_close=Decimal("100")),
    )
    result = calculate_realized_volatility_expected_move_proxy(
        closes, event_horizon_trading_days=4, source="fixture_prices"
    )
    assert result.display_label == "Realized-volatility-based expected-move proxy"
    assert result.daily_log_return_count == 2
    assert result.daily_price_basis.value == "adjusted_close"
    assert result.sample_daily_log_return_stddev == ((Decimal("2").ln() ** 2) * 2).sqrt()
    assert result.expected_move_pct == result.sample_daily_log_return_stddev * Decimal("2")

    with pytest.raises(ValidationError, match="arithmetically inconsistent"):
        result.model_copy(update={"expected_move_pct": Decimal("0.01")}).model_validate(
            result.model_dump() | {"expected_move_pct": Decimal("0.01")}
        )


def test_realized_vol_proxy_rejects_raw_close_alias_and_non_adjusted_basis() -> None:
    with pytest.raises(ValidationError):
        DailyClose(trading_date=date(2026, 1, 2), close=Decimal("100"))
    with pytest.raises(ValidationError):
        DailyClose(
            trading_date=date(2026, 1, 2),
            adjusted_close=Decimal("100"),
            price_basis="raw_close",
        )


def test_realized_vol_proxy_rejects_unsorted_duplicate_or_insufficient_observations() -> None:
    with pytest.raises(ValueError, match="at least three"):
        calculate_realized_volatility_expected_move_proxy(
            (DailyClose(trading_date=date(2026, 1, 2), adjusted_close=Decimal("100")),),
            event_horizon_trading_days=1,
            source="fixture",
        )
    duplicate = (
        DailyClose(trading_date=date(2026, 1, 2), adjusted_close=Decimal("100")),
        DailyClose(trading_date=date(2026, 1, 2), adjusted_close=Decimal("101")),
        DailyClose(trading_date=date(2026, 1, 5), adjusted_close=Decimal("102")),
    )
    with pytest.raises(ValueError, match="strictly ordered"):
        calculate_realized_volatility_expected_move_proxy(
            duplicate, event_horizon_trading_days=1, source="fixture"
        )


def test_volatility_arithmetic_ignores_ambient_decimal_precision() -> None:
    snapshot = chain(
        quote(OptionType.CALL, "100", "2", "2.2"),
        quote(OptionType.PUT, "100", "2", "2.2"),
    )
    closes = (
        DailyClose(trading_date=date(2026, 1, 2), adjusted_close=Decimal("3")),
        DailyClose(trading_date=date(2026, 1, 5), adjusted_close=Decimal("4")),
        DailyClose(trading_date=date(2026, 1, 6), adjusted_close=Decimal("5")),
    )
    with localcontext() as context:
        context.prec = 8
        low_move = calculate_atm_straddle_implied_move(
            snapshot,
            event_window=window(),
            information_cutoff_at=CUTOFF,
        )
        low_proxy = calculate_realized_volatility_expected_move_proxy(
            closes, event_horizon_trading_days=1, source="fixture"
        )
    with localcontext() as context:
        context.prec = 50
        high_move = calculate_atm_straddle_implied_move(
            snapshot,
            event_window=window(),
            information_cutoff_at=CUTOFF,
        )
        high_proxy = calculate_realized_volatility_expected_move_proxy(
            closes, event_horizon_trading_days=1, source="fixture"
        )

    assert low_move == high_move
    assert low_proxy == high_proxy


def test_exact_cutoff_excludes_same_day_post_release_snapshot() -> None:
    pre = chain(
        quote(OptionType.CALL, "100", "2", "2.2"),
        quote(OptionType.PUT, "100", "2", "2.2"),
    )
    post = pre.model_copy(
        update={"as_of": datetime(2026, 1, 6, 22, 0, tzinfo=timezone.utc)}
    )

    selected = select_last_pre_event_snapshot(
        (post, pre),
        information_cutoff_at=CUTOFF,
    )
    assert selected == pre
    with pytest.raises(OptionSelectionError, match="after the information cutoff"):
        calculate_atm_straddle_implied_move(
            post,
            event_window=window(),
            information_cutoff_at=CUTOFF,
        )
