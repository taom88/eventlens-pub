from datetime import date

import pytest

from eventlens.domain.events import (
    EarningsEvent,
    EventTiming,
    EventTimingResolutionError,
    resolve_event_window,
)


DATES = (
    date(2026, 1, 2),
    date(2026, 1, 5),
    date(2026, 1, 6),
    date(2026, 1, 7),
    date(2026, 1, 8),
    date(2026, 1, 9),
    date(2026, 1, 12),
    date(2026, 1, 13),
)


def test_after_market_close_uses_event_close_and_next_session() -> None:
    result = resolve_event_window(
        EarningsEvent(ticker="demo", event_date=date(2026, 1, 6), timing=EventTiming.AFTER_MARKET_CLOSE),
        DATES,
    )

    assert result.reference_trading_date == date(2026, 1, 6)
    assert result.first_post_event_trading_date == date(2026, 1, 7)
    assert result.fifth_post_event_trading_date == date(2026, 1, 13)
    assert result.warnings == ()


def test_before_market_open_uses_previous_close_and_event_session() -> None:
    result = resolve_event_window(
        EarningsEvent(ticker="DEMO", event_date=date(2026, 1, 6), timing=EventTiming.BEFORE_MARKET_OPEN),
        DATES,
    )

    assert result.reference_trading_date == date(2026, 1, 5)
    assert result.first_post_event_trading_date == date(2026, 1, 6)


def test_unknown_timing_requires_explicit_analyst_convention() -> None:
    event = EarningsEvent(ticker="DEMO", event_date=date(2026, 1, 6), timing=EventTiming.UNKNOWN)
    with pytest.raises(EventTimingResolutionError, match="requires analyst-confirmed"):
        resolve_event_window(event, DATES)

    result = resolve_event_window(
        event,
        DATES,
        analyst_confirmed_unknown_timing_as=EventTiming.AFTER_MARKET_CLOSE,
    )
    assert result.reported_timing is EventTiming.UNKNOWN
    assert result.effective_timing is EventTiming.AFTER_MARKET_CLOSE
    assert result.warnings[0].code == "unknown_timing_analyst_convention"


def test_resolver_rejects_unsorted_or_duplicate_provider_dates() -> None:
    event = EarningsEvent(ticker="DEMO", event_date=date(2026, 1, 6), timing=EventTiming.AFTER_MARKET_CLOSE)
    with pytest.raises(EventTimingResolutionError, match="strictly increasing"):
        resolve_event_window(event, (date(2026, 1, 6), date(2026, 1, 6)))


def test_fifth_session_missing_is_visible_not_inferred() -> None:
    result = resolve_event_window(
        EarningsEvent(ticker="DEMO", event_date=date(2026, 1, 6), timing=EventTiming.AFTER_MARKET_CLOSE),
        DATES[:5],
    )
    assert result.fifth_post_event_trading_date is None
    assert result.warnings[-1].code == "fifth_post_event_session_unavailable"
