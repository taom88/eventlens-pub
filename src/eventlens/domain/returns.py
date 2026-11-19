"""Deterministic realized price moves for selected earnings-event windows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from pydantic import Field, model_validator

from ._base import ONE, ZERO, CalculationWarning, DomainModel
from ._decimal import DECIMAL_CALCULATION_VERSION, canonical_decimal_context
from .events import EventWindowResolution


class PriceObservation(DomainModel):
    trading_date: date
    close: Decimal = Field(gt=ZERO)
    open: Decimal | None = Field(default=None, gt=ZERO)


class RealizedReturn(DomainModel):
    """A return that makes signed and absolute quantities unambiguous."""

    window: str
    signed_return_pct: Decimal
    absolute_move_pct: Decimal = Field(ge=ZERO)
    start_trading_date: date
    end_trading_date: date

    @model_validator(mode="after")
    def arithmetic_and_dates_are_consistent(self) -> "RealizedReturn":
        if self.end_trading_date <= self.start_trading_date:
            raise ValueError("realized-return end date must follow start date")
        with canonical_decimal_context():
            expected_absolute = abs(self.signed_return_pct)
        if self.absolute_move_pct != expected_absolute:
            raise ValueError("absolute_move_pct must equal abs(signed_return_pct)")
        return self


class RealizedMoveAnalysis(DomainModel):
    calculation_version: str = DECIMAL_CALCULATION_VERSION
    calculation_method: str = "close_to_close_realized_event_return"
    reference_trading_date: date
    reference_close: Decimal = Field(gt=ZERO)
    first_post_event_trading_date: date | None = None
    fifth_post_event_trading_date: date | None = None
    one_day: RealizedReturn | None = None
    five_day: RealizedReturn | None = None
    overnight_gap_pct: Decimal | None = None
    first_session_intraday_pct: Decimal | None = None
    warnings: tuple[CalculationWarning, ...] = ()


def _return(start: Decimal, end: Decimal) -> Decimal:
    with canonical_decimal_context():
        return (end / start) - ONE


def calculate_realized_moves(
    reference: PriceObservation,
    post_event_sessions: Sequence[PriceObservation],
    *,
    event_window: EventWindowResolution,
) -> RealizedMoveAnalysis:
    """Calculate exact resolved 1d/5d returns and first-session decomposition.

    The event window is the canonical selection of reference, first, and fifth
    trading sessions.  Observations are selected by those dates, never by
    arbitrary sequence position.  A missing intermediate resolved session
    blocks the five-day metric rather than silently using a later observation.
    """

    sessions = tuple(post_event_sessions)
    if reference.trading_date != event_window.reference_trading_date:
        raise ValueError("reference observation does not match resolved reference trading date")
    if any(
        left.trading_date >= right.trading_date for left, right in zip(sessions, sessions[1:])
    ):
        raise ValueError("post_event_sessions must be strictly ordered by trading_date")
    if sessions and sessions[0].trading_date <= reference.trading_date:
        raise ValueError("post-event sessions must occur after the reference session")

    warnings: list[CalculationWarning] = []
    observations_by_date = {item.trading_date: item for item in sessions}
    first = observations_by_date.get(event_window.first_post_event_trading_date)
    if first is None:
        warnings.append(
            CalculationWarning(
                code="first_post_event_observation_missing",
                message=(
                    "No price observation was supplied for resolved first post-event session "
                    f"{event_window.first_post_event_trading_date.isoformat()}."
                ),
            )
        )
        return RealizedMoveAnalysis(
            reference_trading_date=reference.trading_date,
            reference_close=reference.close,
            first_post_event_trading_date=event_window.first_post_event_trading_date,
            fifth_post_event_trading_date=event_window.fifth_post_event_trading_date,
            warnings=tuple(warnings),
        )

    one_day_signed = _return(reference.close, first.close)
    with canonical_decimal_context():
        one_day_absolute = abs(one_day_signed)
    one_day = RealizedReturn(
        window="first_post_event_close_vs_reference_close",
        signed_return_pct=one_day_signed,
        absolute_move_pct=one_day_absolute,
        start_trading_date=reference.trading_date,
        end_trading_date=first.trading_date,
    )
    overnight_gap = None
    intraday = None
    if first.open is None:
        warnings.append(
            CalculationWarning(
                code="first_session_open_missing",
                message="Overnight-gap and first-session intraday decomposition are unavailable.",
            )
        )
    else:
        overnight_gap = _return(reference.close, first.open)
        intraday = _return(first.open, first.close)

    five_day = None
    fifth_date = event_window.fifth_post_event_trading_date
    expected_five_day_dates = event_window.resolved_post_event_trading_dates
    missing_resolved_dates = tuple(
        item for item in expected_five_day_dates if item not in observations_by_date
    )
    if event_window.fifth_post_event_trading_date is None:
        warnings.append(
            CalculationWarning(
                code="fifth_post_event_session_unresolved",
                message="The supplied trading-date resolution does not include a fifth post-event session.",
            )
        )
    elif missing_resolved_dates:
        missing_dates = ", ".join(item.isoformat() for item in missing_resolved_dates)
        warnings.append(
            CalculationWarning(
                code="resolved_post_event_observations_missing",
                message=(
                    "Five-day realized movement is unavailable because resolved post-event "
                    f"sessions are missing: {missing_dates}."
                ),
            )
        )
    else:
        fifth = observations_by_date[event_window.fifth_post_event_trading_date]
        five_day_signed = _return(reference.close, fifth.close)
        with canonical_decimal_context():
            five_day_absolute = abs(five_day_signed)
        five_day = RealizedReturn(
            window="fifth_post_event_close_vs_reference_close",
            signed_return_pct=five_day_signed,
            absolute_move_pct=five_day_absolute,
            start_trading_date=reference.trading_date,
            end_trading_date=fifth.trading_date,
        )
    return RealizedMoveAnalysis(
        reference_trading_date=reference.trading_date,
        reference_close=reference.close,
        first_post_event_trading_date=event_window.first_post_event_trading_date,
        fifth_post_event_trading_date=fifth_date,
        one_day=one_day,
        five_day=five_day,
        overnight_gap_pct=overnight_gap,
        first_session_intraday_pct=intraday,
        warnings=tuple(warnings),
    )
