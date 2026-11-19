"""Earnings-event timing and trading-session resolution.

This module intentionally accepts an ordered set of trading dates from a
provider.  It never imports or guesses a market calendar.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from ._base import CalculationWarning, DomainModel


class EventTiming(StrEnum):
    BEFORE_MARKET_OPEN = "before_market_open"
    AFTER_MARKET_CLOSE = "after_market_close"
    DURING_MARKET_HOURS = "during_market_hours"
    UNKNOWN = "unknown"


class EarningsEvent(DomainModel):
    ticker: str = Field(min_length=1)
    event_date: date
    timing: EventTiming
    fiscal_quarter: str | None = None

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker must not be blank")
        return normalized


class EventWindowResolution(DomainModel):
    """The date convention used by all event-return calculations."""

    method: str = "supplied_ordered_trading_dates"
    event_date: date
    reported_timing: EventTiming
    effective_timing: EventTiming
    reference_trading_date: date
    first_post_event_trading_date: date
    fifth_post_event_trading_date: date | None = None
    resolved_post_event_trading_dates: tuple[date, ...] = Field(min_length=1)
    warnings: tuple[CalculationWarning, ...] = ()

    @model_validator(mode="after")
    def resolved_post_event_dates_must_match_window(self) -> "EventWindowResolution":
        dates = self.resolved_post_event_trading_dates
        if dates[0] != self.first_post_event_trading_date:
            raise ValueError(
                "resolved post-event dates must begin with first_post_event_trading_date"
            )
        if any(left >= right for left, right in zip(dates, dates[1:])):
            raise ValueError("resolved post-event dates must be strictly increasing")
        if self.fifth_post_event_trading_date is None:
            if len(dates) >= 5:
                raise ValueError(
                    "fifth_post_event_trading_date is required when five post-event dates resolve"
                )
        elif len(dates) < 5 or dates[4] != self.fifth_post_event_trading_date:
            raise ValueError(
                "fifth_post_event_trading_date must be the fifth resolved post-event date"
            )
        return self


class EventTimingResolutionError(ValueError):
    """Raised when supplied sessions cannot support the selected convention."""


def _validate_ordered_trading_dates(trading_dates: Sequence[date]) -> tuple[date, ...]:
    dates = tuple(trading_dates)
    if not dates:
        raise EventTimingResolutionError("at least one supplied trading date is required")
    if any(not isinstance(value, date) for value in dates):
        raise TypeError("trading_dates must contain date values")
    if any(left >= right for left, right in zip(dates, dates[1:])):
        raise EventTimingResolutionError(
            "trading_dates must be strictly increasing and contain no duplicates"
        )
    return dates


def _index_of_event_session(event_date: date, trading_dates: tuple[date, ...]) -> int:
    try:
        return trading_dates.index(event_date)
    except ValueError as error:
        raise EventTimingResolutionError(
            "event date must be present in supplied trading_dates for this timing convention"
        ) from error


def resolve_event_window(
    event: EarningsEvent,
    trading_dates: Sequence[date],
    *,
    analyst_confirmed_unknown_timing_as: EventTiming | None = None,
) -> EventWindowResolution:
    """Resolve reference and post-event sessions from explicit provider dates.

    ``UNKNOWN`` timing cannot be resolved implicitly.  Callers must pass an
    analyst-confirmed BMO or AMC convention; the result retains the original
    unknown timing and a warning so that the approximation is auditable.
    """

    dates = _validate_ordered_trading_dates(trading_dates)
    warnings: tuple[CalculationWarning, ...] = ()
    effective_timing = event.timing

    if event.timing is EventTiming.UNKNOWN:
        if analyst_confirmed_unknown_timing_as not in {
            EventTiming.BEFORE_MARKET_OPEN,
            EventTiming.AFTER_MARKET_CLOSE,
        }:
            raise EventTimingResolutionError(
                "unknown event timing requires analyst-confirmed before_market_open "
                "or after_market_close convention"
            )
        effective_timing = analyst_confirmed_unknown_timing_as
        warnings = (
            CalculationWarning(
                code="unknown_timing_analyst_convention",
                message=(
                    "Announcement timing is unknown; event window uses the "
                    f"analyst-confirmed {effective_timing.value} convention."
                ),
            ),
        )

    event_index = _index_of_event_session(event.event_date, dates)
    if effective_timing is EventTiming.BEFORE_MARKET_OPEN:
        if event_index == 0:
            raise EventTimingResolutionError(
                "before-market-open event needs a preceding supplied trading date"
            )
        reference_index = event_index - 1
        first_post_index = event_index
    elif effective_timing is EventTiming.AFTER_MARKET_CLOSE:
        reference_index = event_index
        first_post_index = event_index + 1
    elif effective_timing is EventTiming.DURING_MARKET_HOURS:
        # The pre-event close is the preceding close; the event-date close is
        # the first session that can contain the reaction.  This is explicit
        # rather than silently treating an intraday announcement as AMC.
        if event_index == 0:
            raise EventTimingResolutionError(
                "during-market-hours event needs a preceding supplied trading date"
            )
        reference_index = event_index - 1
        first_post_index = event_index
        warnings = warnings + (
            CalculationWarning(
                code="during_market_hours_window",
                message=(
                    "During-market-hours timing uses the preceding close as "
                    "reference and the event-date close as first post-event close."
                ),
            ),
        )
    else:  # Defensive guard for future enum values.
        raise EventTimingResolutionError(f"unsupported event timing: {effective_timing}")

    if first_post_index >= len(dates):
        raise EventTimingResolutionError(
            "supplied trading_dates do not include a first post-event session"
        )

    fifth_post_index = first_post_index + 4
    fifth_post_date = dates[fifth_post_index] if fifth_post_index < len(dates) else None
    resolved_post_event_dates = dates[first_post_index : min(first_post_index + 5, len(dates))]
    if fifth_post_date is None:
        warnings = warnings + (
            CalculationWarning(
                code="fifth_post_event_session_unavailable",
                message="Supplied trading dates do not extend through the fifth post-event session.",
            ),
        )

    return EventWindowResolution(
        event_date=event.event_date,
        reported_timing=event.timing,
        effective_timing=effective_timing,
        reference_trading_date=dates[reference_index],
        first_post_event_trading_date=dates[first_post_index],
        fifth_post_event_trading_date=fifth_post_date,
        resolved_post_event_trading_dates=resolved_post_event_dates,
        warnings=warnings,
    )
