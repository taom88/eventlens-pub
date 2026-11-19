"""Typed, non-causal market-reaction windows for v4 context cards.

The calculation deliberately consumes supplied daily sessions and an optional
after-hours quote.  It does not fetch prices, infer an after-hours value, or
make a claim about why a security moved.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.domain._base import ONE, ZERO, CalculationWarning
from eventlens.domain._decimal import DECIMAL_CALCULATION_VERSION, canonical_decimal_context
from eventlens.domain.events import EventWindowResolution
from eventlens.domain.returns import PriceObservation, calculate_realized_moves

from .attribution import (
    AttributionWindow,
    ReactionAttribution,
    validate_attribution_horizons,
)


REACTION_METHODOLOGY_VERSION = "market-reaction-v4"
REACTION_METHODOLOGY = "next-session-open-vs-reference-close-with-supplemental-after-hours"


class _ReactionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReactionPriceSet(_ReactionModel):
    """Prices selected for documented post-event reaction windows.

    ``next_open`` duplicates the first post-event observation's ``open`` so
    consumers of the canonical card do not need to infer that price from an
    embedded daily bar.
    """

    reference: PriceObservation
    after_hours: Decimal | None = Field(default=None, gt=ZERO)
    t_plus_1: PriceObservation | None = None
    t_plus_2: PriceObservation | None = None
    t_plus_5: PriceObservation | None = None
    next_open: Decimal | None = Field(default=None, gt=ZERO)
    after_hours_role: Literal["supplemental_only"] = "supplemental_only"

    @model_validator(mode="after")
    def prices_follow_reference_session(self) -> "ReactionPriceSet":
        for name in ("t_plus_1", "t_plus_2", "t_plus_5"):
            observation = getattr(self, name)
            if observation is not None and observation.trading_date <= self.reference.trading_date:
                raise ValueError(f"{name} must occur after the reference session")
        if self.t_plus_1 is None:
            if self.next_open is not None:
                raise ValueError("next_open requires the T+1 price observation")
        elif self.t_plus_1.open != self.next_open:
            raise ValueError("next_open must match the T+1 price observation open")
        return self


class ReactionReturnSet(_ReactionModel):
    """Signed decimal returns for AH and post-event reaction windows.

    A null value means that its price window was unavailable or incomplete; it
    never represents a substituted zero return.
    """

    after_hours_pct: Decimal | None = None
    next_session_open_vs_reference_close_pct: Decimal | None = None
    t_plus_1_pct: Decimal | None = None
    t_plus_2_pct: Decimal | None = None
    t_plus_5_pct: Decimal | None = None
    # Backward-compatible alias for older v4 drafts.  New canonical consumers
    # should read ``next_session_open_vs_reference_close_pct``.
    overnight_gap_pct: Decimal | None = None
    first_session_intraday_pct: Decimal | None = None


class BenchmarkReaction(_ReactionModel):
    """The matched benchmark reaction used for arithmetic context only."""

    ticker: str
    prices: ReactionPriceSet
    returns: ReactionReturnSet
    methodology: str = REACTION_METHODOLOGY
    methodology_version: str = REACTION_METHODOLOGY_VERSION
    warnings: tuple[CalculationWarning, ...] = ()

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("benchmark ticker must not be blank")
        return normalized


class MarketReaction(_ReactionModel):
    """A factual market reaction, explicitly separate from attribution."""

    fiscal_period: str
    event_window: EventWindowResolution
    prices: ReactionPriceSet
    returns: ReactionReturnSet
    benchmark: BenchmarkReaction | None = None
    benchmark_adjusted_returns: ReactionReturnSet | None = None
    attributions: tuple[ReactionAttribution, ...] = ()
    methodology: str = REACTION_METHODOLOGY
    methodology_version: str = REACTION_METHODOLOGY_VERSION
    calculation_version: str = DECIMAL_CALCULATION_VERSION
    warnings: tuple[CalculationWarning, ...] = ()

    @field_validator("fiscal_period")
    @classmethod
    def fiscal_period_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fiscal_period must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def benchmark_adjustment_requires_benchmark(self) -> "MarketReaction":
        if self.benchmark is None and self.benchmark_adjusted_returns is not None:
            raise ValueError("benchmark_adjusted_returns requires benchmark")
        if self.benchmark is not None and self.benchmark_adjusted_returns is None:
            raise ValueError("benchmark requires benchmark_adjusted_returns")
        if self.benchmark_adjusted_returns is not None and any(
            getattr(self.benchmark_adjusted_returns, name) is not None
            for name in (
                "next_session_open_vs_reference_close_pct",
                "overnight_gap_pct",
                "first_session_intraday_pct",
            )
        ):
            raise ValueError(
                "benchmark-adjusted returns cannot include first-session open decomposition"
            )
        validate_attribution_horizons(self.attributions)
        for attribution in self.attributions:
            return_field = {
                AttributionWindow.AFTER_HOURS: "after_hours_pct",
                AttributionWindow.T_PLUS_1: "t_plus_1_pct",
                AttributionWindow.T_PLUS_2: "t_plus_2_pct",
                AttributionWindow.T_PLUS_5: "t_plus_5_pct",
            }[attribution.window]
            if getattr(self.returns, return_field) is None:
                raise ValueError(
                    f"{attribution.window.value} attribution requires a measured reaction return"
                )
        self._assert_prices_match_event_window(self.prices, label="security")
        canonical = self.returns.next_session_open_vs_reference_close_pct
        if canonical != self.returns.overnight_gap_pct:
            raise ValueError("canonical next-open return must equal overnight_gap_pct")
        if self.prices.next_open is None:
            if canonical is not None:
                raise ValueError("canonical next-open return requires next_open")
        else:
            expected = _return(self.prices.reference.close, self.prices.next_open)
            if canonical != expected:
                raise ValueError("canonical next-open return does not match serialized prices")
        if self.benchmark is not None:
            self._assert_prices_match_event_window(self.benchmark.prices, label="benchmark")
        return self

    def _assert_prices_match_event_window(
        self,
        prices: ReactionPriceSet,
        *,
        label: str,
    ) -> None:
        """Keep serialized reaction endpoints tied to the resolved sessions.

        ``ReactionPriceSet`` is reusable on its own, so it only verifies that
        observations follow its reference.  Once it is attached to a market
        reaction, each named window must instead identify the exact session
        selected by the event-time convention.
        """

        if prices.reference.trading_date != self.event_window.reference_trading_date:
            raise ValueError(f"{label} reference price does not match resolved reference session")
        expected_dates = {
            "t_plus_1": self.event_window.first_post_event_trading_date,
            "t_plus_2": (
                self.event_window.resolved_post_event_trading_dates[1]
                if len(self.event_window.resolved_post_event_trading_dates) >= 2
                else None
            ),
            "t_plus_5": self.event_window.fifth_post_event_trading_date,
        }
        for field, expected_date in expected_dates.items():
            observation = getattr(prices, field)
            if observation is not None and observation.trading_date != expected_date:
                raise ValueError(
                    f"{label} {field} price does not match its resolved post-event session"
                )


def _return(start: Decimal, end: Decimal) -> Decimal:
    with canonical_decimal_context():
        return (end / start) - ONE


def _validate_observations(
    reference: PriceObservation,
    post_event_sessions: Sequence[PriceObservation],
    event_window: EventWindowResolution,
) -> tuple[PriceObservation, ...]:
    sessions = tuple(post_event_sessions)
    if reference.trading_date != event_window.reference_trading_date:
        raise ValueError("reference observation does not match resolved reference trading date")
    if any(left.trading_date >= right.trading_date for left, right in zip(sessions, sessions[1:])):
        raise ValueError("post_event_sessions must be strictly ordered by trading_date")
    if sessions and sessions[0].trading_date <= reference.trading_date:
        raise ValueError("post-event sessions must occur after the reference session")
    return sessions


def _prices_and_returns(
    reference: PriceObservation,
    post_event_sessions: Sequence[PriceObservation],
    *,
    event_window: EventWindowResolution,
    after_hours_price: Decimal | None,
) -> tuple[ReactionPriceSet, ReactionReturnSet, tuple[CalculationWarning, ...]]:
    sessions = _validate_observations(reference, post_event_sessions, event_window)
    by_date = {item.trading_date: item for item in sessions}
    dates = event_window.resolved_post_event_trading_dates

    selected = {
        "t_plus_1": by_date.get(event_window.first_post_event_trading_date),
        "t_plus_2": by_date.get(dates[1]) if len(dates) >= 2 else None,
        "t_plus_5": by_date.get(event_window.fifth_post_event_trading_date)
        if event_window.fifth_post_event_trading_date is not None
        else None,
    }
    canonical = calculate_realized_moves(reference, sessions, event_window=event_window)
    warnings: list[CalculationWarning] = list(canonical.warnings)
    if after_hours_price is None:
        warnings.append(
            CalculationWarning(
                code="after_hours_price_missing",
                message="After-hours price is unavailable; after-hours return remains null.",
            )
        )

    returns: dict[str, Decimal | None] = {
        "after_hours_pct": _return(reference.close, after_hours_price)
        if after_hours_price is not None
        else None,
        "t_plus_1_pct": canonical.one_day.signed_return_pct if canonical.one_day else None,
        "t_plus_2_pct": None,
        "t_plus_5_pct": canonical.five_day.signed_return_pct if canonical.five_day else None,
        "next_session_open_vs_reference_close_pct": None,
        "overnight_gap_pct": canonical.overnight_gap_pct,
        "first_session_intraday_pct": canonical.first_session_intraday_pct,
    }
    returns["next_session_open_vs_reference_close_pct"] = canonical.overnight_gap_pct
    first = selected["t_plus_1"]

    # T+2 is a context-card-specific extension; T+1/T+5 and first-session
    # decomposition above are sourced exclusively from the domain calculator.
    if len(dates) < 2:
        warnings.append(
            CalculationWarning(
                code="t_plus_2_session_unresolved",
                message="T+2 return is unavailable because its session is unresolved.",
            )
        )
    elif dates[1] not in by_date:
        warnings.append(
            CalculationWarning(
                code="t_plus_2_observations_missing",
                message="T+2 return is unavailable because its resolved observation is missing.",
            )
        )
    else:
        returns["t_plus_2_pct"] = _return(reference.close, by_date[dates[1]].close)
    if event_window.fifth_post_event_trading_date is not None and (
        any(day not in by_date for day in event_window.resolved_post_event_trading_dates)
    ):
        warnings.append(
            CalculationWarning(
                code="t_plus_5_observations_missing",
                message="T+5 return is unavailable because a resolved post-event observation is missing.",
            )
        )

    return (
        ReactionPriceSet(
            reference=reference,
            after_hours=after_hours_price,
            next_open=first.open if first is not None else None,
            **selected,
        ),
        ReactionReturnSet(**returns),
        tuple(warnings),
    )


def benchmark_adjust(
    security_returns: ReactionReturnSet,
    benchmark_returns: ReactionReturnSet,
) -> ReactionReturnSet:
    """Return security-minus-benchmark performance for matching windows only.

    AH and close-to-close horizons have a clear common benchmark convention.
    The first-session open decomposition remains raw context and is therefore
    deliberately not benchmark-adjusted.
    """

    with canonical_decimal_context():
        return ReactionReturnSet(
            **{
                name: None
                if (security := getattr(security_returns, name)) is None
                or (benchmark := getattr(benchmark_returns, name)) is None
                else security - benchmark
                for name in (
                    "after_hours_pct",
                    "t_plus_1_pct",
                    "t_plus_2_pct",
                    "t_plus_5_pct",
                )
            }
        )


def calculate_market_reaction(
    reference: PriceObservation,
    post_event_sessions: Sequence[PriceObservation],
    *,
    event_window: EventWindowResolution,
    fiscal_period: str,
    after_hours_price: Decimal | None = None,
    benchmark_reference: PriceObservation | None = None,
    benchmark_post_event_sessions: Sequence[PriceObservation] | None = None,
    benchmark_after_hours_price: Decimal | None = None,
    benchmark_ticker: str | None = None,
) -> MarketReaction:
    """Calculate AH/T+1/T+2/T+5 factual reaction windows from supplied prices.

    BMO and AMC timing are mapped by ``resolve_event_window``; this function
    does not reinterpret sessions.  Benchmark inputs are optional as a group.
    When present, relative returns are exactly security return minus benchmark
    return, not an attribution or model-based abnormal return.
    """

    prices, returns, warnings = _prices_and_returns(
        reference,
        post_event_sessions,
        event_window=event_window,
        after_hours_price=after_hours_price,
    )
    supplied_benchmark_parts = (
        benchmark_reference is not None,
        benchmark_post_event_sessions is not None,
    )
    if any(supplied_benchmark_parts) and not all(supplied_benchmark_parts):
        raise ValueError("benchmark reference and post-event sessions must be supplied together")
    if all(supplied_benchmark_parts) and not benchmark_ticker:
        raise ValueError("benchmark ticker is required when benchmark prices are supplied")

    benchmark: BenchmarkReaction | None = None
    adjusted: ReactionReturnSet | None = None
    if benchmark_reference is not None and benchmark_post_event_sessions is not None:
        benchmark_prices, benchmark_returns, benchmark_warnings = _prices_and_returns(
            benchmark_reference,
            benchmark_post_event_sessions,
            event_window=event_window,
            after_hours_price=benchmark_after_hours_price,
        )
        benchmark = BenchmarkReaction(
            ticker=benchmark_ticker,
            prices=benchmark_prices,
            returns=benchmark_returns,
            warnings=benchmark_warnings,
        )
        adjusted = benchmark_adjust(returns, benchmark_returns)

    return MarketReaction(
        fiscal_period=fiscal_period,
        event_window=event_window,
        prices=prices,
        returns=returns,
        benchmark=benchmark,
        benchmark_adjusted_returns=adjusted,
        warnings=warnings,
    )


__all__ = [
    "REACTION_METHODOLOGY",
    "REACTION_METHODOLOGY_VERSION",
    "BenchmarkReaction",
    "MarketReaction",
    "ReactionPriceSet",
    "ReactionReturnSet",
    "ReactionAttribution",
    "benchmark_adjust",
    "calculate_market_reaction",
]
