"""Deterministic expectation measures for earnings-event volatility."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from ._base import ONE, ZERO, CalculationWarning, DomainModel
from ._decimal import DECIMAL_CALCULATION_VERSION, canonical_decimal_context
from .events import EventWindowResolution


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class OptionSnapshotKind(StrEnum):
    END_OF_DAY = "end_of_day"
    INTRADAY = "intraday"


class PreEventSnapshotAlignment(StrEnum):
    """How confidently a source snapshot is aligned to the pre-release cutoff."""

    VERIFIED_PRE_EVENT = "verified_pre_event"
    APPROXIMATE_PRE_EVENT = "approximate_pre_event"
    UNVERIFIED = "unverified"


class ExpectationMeasureKind(StrEnum):
    ATM_STRADDLE_DERIVED_IMPLIED_MOVE = "atm_straddle_derived_implied_move"
    REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY = "realized_volatility_expected_move_proxy"

    @property
    def display_label(self) -> str:
        return {
            self.ATM_STRADDLE_DERIVED_IMPLIED_MOVE: "ATM-straddle-derived implied move",
            self.REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY: (
                "Realized-volatility-based expected-move proxy"
            ),
        }[self]


class DailyPriceBasis(StrEnum):
    """Only corporate-action-adjusted closes are valid proxy inputs."""

    ADJUSTED_CLOSE = "adjusted_close"


class OptionQuote(DomainModel):
    option_type: OptionType
    strike: Decimal = Field(gt=ZERO)
    expiration: date
    bid: Decimal = Field(ge=ZERO)
    ask: Decimal = Field(ge=ZERO)

    @model_validator(mode="after")
    def ask_must_not_be_below_bid(self) -> "OptionQuote":
        if self.ask < self.bid:
            raise ValueError("option ask must be greater than or equal to bid")
        return self

    @property
    def mid(self) -> Decimal:
        with canonical_decimal_context():
            return (self.bid + self.ask) / Decimal("2")


class OptionChainSnapshot(DomainModel):
    ticker: str = Field(min_length=1)
    snapshot_date: date
    as_of: datetime
    snapshot_kind: OptionSnapshotKind
    pre_event_alignment: PreEventSnapshotAlignment = PreEventSnapshotAlignment.UNVERIFIED
    spot: Decimal = Field(gt=ZERO)
    quotes: tuple[OptionQuote, ...] = Field(min_length=1)
    source: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("option snapshot as_of must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def contracts_must_be_unique(self) -> "OptionChainSnapshot":
        contracts = [
            (quote.option_type, quote.strike, quote.expiration) for quote in self.quotes
        ]
        if len(contracts) != len(set(contracts)):
            raise ValueError("option snapshot contains duplicate option contracts")
        return self


class ATMStraddleSelection(DomainModel):
    """Validated call/put pair selected at a deterministic eligible strike."""

    expiration: date
    strike: Decimal
    call: OptionQuote
    put: OptionQuote
    expiry_selection_rule: str = "nearest_expiration_on_or_after_required_expiration_date"
    strike_selection_rule: str = "nearest_strike_to_reference_spot_then_lower_strike"


class ATMStraddleImpliedMove(DomainModel):
    calculation_version: str = DECIMAL_CALCULATION_VERSION
    calculation_method: ExpectationMeasureKind = (
        ExpectationMeasureKind.ATM_STRADDLE_DERIVED_IMPLIED_MOVE
    )
    display_label: str = ExpectationMeasureKind.ATM_STRADDLE_DERIVED_IMPLIED_MOVE.display_label
    snapshot_as_of: datetime
    information_cutoff_at: datetime
    snapshot_lag_seconds: Decimal = Field(ge=ZERO)
    snapshot_kind: OptionSnapshotKind
    pre_event_alignment: PreEventSnapshotAlignment
    snapshot_source: str
    event_date: date
    reference_spot: Decimal = Field(gt=ZERO)
    expiration: date
    strike: Decimal = Field(gt=ZERO)
    call_bid: Decimal = Field(ge=ZERO)
    call_ask: Decimal = Field(ge=ZERO)
    call_mid: Decimal = Field(ge=ZERO)
    put_bid: Decimal = Field(ge=ZERO)
    put_ask: Decimal = Field(ge=ZERO)
    put_mid: Decimal = Field(ge=ZERO)
    straddle_cost: Decimal = Field(ge=ZERO)
    expected_move_pct: Decimal = Field(ge=ZERO)
    approximation_note: str | None = None

    @field_validator("snapshot_as_of", "information_cutoff_at")
    @classmethod
    def result_timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("implied-move timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def arithmetic_is_consistent(self) -> "ATMStraddleImpliedMove":
        if self.calculation_method is not ExpectationMeasureKind.ATM_STRADDLE_DERIVED_IMPLIED_MOVE:
            raise ValueError("implied-move calculation method is inconsistent")
        if self.display_label != self.calculation_method.display_label:
            raise ValueError("implied-move display label is inconsistent")
        if self.call_ask < self.call_bid or self.put_ask < self.put_bid:
            raise ValueError("implied-move output contains crossed option quotes")
        with canonical_decimal_context():
            call_mid = (self.call_bid + self.call_ask) / Decimal("2")
            put_mid = (self.put_bid + self.put_ask) / Decimal("2")
            cost = call_mid + put_mid
            move = cost / self.reference_spot
        lag = Decimal(str((self.information_cutoff_at - self.snapshot_as_of).total_seconds()))
        if lag < ZERO:
            raise ValueError("snapshot cannot occur after information cutoff")
        if (
            self.call_mid != call_mid
            or self.put_mid != put_mid
            or self.straddle_cost != cost
            or self.expected_move_pct != move
            or self.snapshot_lag_seconds != lag
        ):
            raise ValueError("implied-move derived fields are arithmetically inconsistent")
        return self


class DailyClose(DomainModel):
    trading_date: date
    price_basis: DailyPriceBasis = DailyPriceBasis.ADJUSTED_CLOSE
    # Raw close is deliberately not accepted: corporate actions must not
    # masquerade as realized volatility in a labeled adjusted-close proxy.
    adjusted_close: Decimal = Field(gt=ZERO)


class RealizedVolatilityExpectedMoveProxy(DomainModel):
    calculation_version: str = DECIMAL_CALCULATION_VERSION
    calculation_method: ExpectationMeasureKind = (
        ExpectationMeasureKind.REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY
    )
    display_label: str = (
        ExpectationMeasureKind.REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY.display_label
    )
    proxy_type: str = "historical_realized_volatility"
    daily_price_basis: DailyPriceBasis = DailyPriceBasis.ADJUSTED_CLOSE
    basis_as_of_date: date
    staleness_trading_days: int = Field(ge=0)
    refresh_required: bool = True
    daily_log_return_count: int = Field(ge=2)
    sample_daily_log_return_stddev: Decimal = Field(ge=ZERO)
    event_horizon_trading_days: int = Field(ge=1)
    expected_move_pct: Decimal = Field(ge=ZERO)
    source: str = Field(min_length=1)
    warnings: tuple[CalculationWarning, ...] = ()

    @model_validator(mode="after")
    def arithmetic_is_consistent(self) -> "RealizedVolatilityExpectedMoveProxy":
        if (
            self.calculation_method
            is not ExpectationMeasureKind.REALIZED_VOLATILITY_EXPECTED_MOVE_PROXY
            or self.display_label != self.calculation_method.display_label
            or self.daily_price_basis is not DailyPriceBasis.ADJUSTED_CLOSE
        ):
            raise ValueError("realized-volatility proxy labels are inconsistent")
        with canonical_decimal_context():
            expected = self.sample_daily_log_return_stddev * Decimal(
                self.event_horizon_trading_days
            ).sqrt()
        if self.expected_move_pct != expected:
            raise ValueError("proxy expected_move_pct is arithmetically inconsistent")
        return self


class OptionSelectionError(ValueError):
    """Raised when no valid ATM straddle can be selected from a valid chain."""


def select_atm_straddle(
    snapshot: OptionChainSnapshot,
    *,
    event_window: EventWindowResolution,
    information_cutoff_at: datetime,
    reference_spot: Decimal | None = None,
) -> ATMStraddleSelection:
    """Select the nearest eligible expiration then nearest strike.

    A strike needs both a call and a put.  If strikes are equidistant from
    spot, the lower strike wins; this documented rule makes fixture replay
    stable across input ordering.
    """

    _validate_snapshot_cutoff(snapshot, information_cutoff_at)
    spot = reference_spot if reference_spot is not None else snapshot.spot
    if spot <= ZERO:
        raise ValueError("reference_spot must be greater than zero")

    # An expiration must include the first session capable of reflecting the
    # earnings release. This permits event-day expiry for BMO, but excludes it
    # for AMC where the first reaction session is the following trading day.
    required_expiration = event_window.first_post_event_trading_date
    eligible_expirations = sorted(
        {
            quote.expiration
            for quote in snapshot.quotes
            if quote.expiration >= required_expiration
        }
    )
    if not eligible_expirations:
        raise OptionSelectionError(
            "no option expiration occurs on or after required expiration date"
        )

    for expiration in eligible_expirations:
        by_strike: dict[Decimal, dict[OptionType, OptionQuote]] = defaultdict(dict)
        for quote in snapshot.quotes:
            if quote.expiration == expiration:
                if quote.option_type in by_strike[quote.strike]:
                    raise OptionSelectionError(
                        f"duplicate {quote.option_type.value} quote at strike {quote.strike}"
                    )
                by_strike[quote.strike][quote.option_type] = quote
        paired_strikes = [
            strike
            for strike, quotes in by_strike.items()
            if OptionType.CALL in quotes and OptionType.PUT in quotes
        ]
        if paired_strikes:
            strike = min(paired_strikes, key=lambda candidate: (abs(candidate - spot), candidate))
            quotes = by_strike[strike]
            return ATMStraddleSelection(
                expiration=expiration,
                strike=strike,
                call=quotes[OptionType.CALL],
                put=quotes[OptionType.PUT],
            )

    raise OptionSelectionError("no eligible expiration contains a complete call/put pair")


def calculate_atm_straddle_implied_move(
    snapshot: OptionChainSnapshot,
    *,
    event_window: EventWindowResolution,
    information_cutoff_at: datetime,
    reference_spot: Decimal | None = None,
) -> ATMStraddleImpliedMove:
    """Calculate the PRD's disclosed ATM-straddle expected-move approximation."""

    spot = reference_spot if reference_spot is not None else snapshot.spot
    cutoff = _normalize_cutoff(information_cutoff_at)
    selection = select_atm_straddle(
        snapshot,
        event_window=event_window,
        information_cutoff_at=information_cutoff_at,
        reference_spot=spot,
    )
    with canonical_decimal_context():
        call_mid = selection.call.mid
        put_mid = selection.put.mid
        straddle_cost = call_mid + put_mid
        expected_move = straddle_cost / spot
    notes: list[str] = []
    snapshot_lag_seconds = Decimal(str((cutoff - snapshot.as_of).total_seconds()))
    if snapshot.as_of != cutoff:
        notes.append(
            "Snapshot precedes the desired information cutoff by "
            f"{snapshot_lag_seconds} seconds; calculation is an approximation."
        )
    if snapshot.as_of.date() != event_window.reference_trading_date:
        notes.append(
            "Snapshot date differs from the reference trading date; calculation is an approximation."
        )
    if snapshot.pre_event_alignment is not PreEventSnapshotAlignment.VERIFIED_PRE_EVENT:
        notes.append(
            "Snapshot pre-event alignment is not verified; confirm the source cutoff."
        )
    return ATMStraddleImpliedMove(
        snapshot_as_of=snapshot.as_of,
        information_cutoff_at=cutoff,
        snapshot_lag_seconds=snapshot_lag_seconds,
        snapshot_kind=snapshot.snapshot_kind,
        pre_event_alignment=snapshot.pre_event_alignment,
        snapshot_source=snapshot.source,
        event_date=event_window.event_date,
        reference_spot=spot,
        expiration=selection.expiration,
        strike=selection.strike,
        call_bid=selection.call.bid,
        call_ask=selection.call.ask,
        call_mid=call_mid,
        put_bid=selection.put.bid,
        put_ask=selection.put.ask,
        put_mid=put_mid,
        straddle_cost=straddle_cost,
        expected_move_pct=expected_move,
        approximation_note=" ".join(notes) if notes else None,
    )


def select_last_pre_event_snapshot(
    snapshots: Iterable[OptionChainSnapshot], *, information_cutoff_at: datetime
) -> OptionChainSnapshot:
    """Return the latest supplied snapshot at or before an exact known-data cutoff."""

    cutoff = _normalize_cutoff(information_cutoff_at)
    supported = [item for item in snapshots if item.as_of <= cutoff]
    if not supported:
        raise OptionSelectionError("no option snapshot is on or before information cutoff")
    return max(supported, key=lambda item: item.as_of)


def _validate_snapshot_cutoff(
    snapshot: OptionChainSnapshot, information_cutoff_at: datetime
) -> None:
    cutoff = _normalize_cutoff(information_cutoff_at)
    if snapshot.as_of > cutoff:
        raise OptionSelectionError("option snapshot occurs after the information cutoff")


def _normalize_cutoff(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("information_cutoff_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def calculate_realized_volatility_expected_move_proxy(
    daily_closes: Sequence[DailyClose],
    *,
    event_horizon_trading_days: int,
    source: str,
    basis_as_of_date: date | None = None,
    staleness_trading_days: int = 0,
    refresh_required: bool = True,
) -> RealizedVolatilityExpectedMoveProxy:
    """Calculate a labeled historical-realized-volatility expectation proxy.

    Log returns use Decimal's natural logarithm and the standard deviation uses
    the sample denominator (n - 1), as specified in the PRD.
    """

    observations = tuple(daily_closes)
    if event_horizon_trading_days < 1:
        raise ValueError("event_horizon_trading_days must be at least one")
    if not source.strip():
        raise ValueError("source must not be blank")
    if staleness_trading_days < 0:
        raise ValueError("staleness_trading_days cannot be negative")
    if len(observations) < 3:
        raise ValueError("at least three closes (two log returns) are required")
    if any(
        left.trading_date >= right.trading_date
        for left, right in zip(observations, observations[1:])
    ):
        raise ValueError("daily_closes must be strictly ordered by trading_date")

    with canonical_decimal_context():
        returns = tuple(
            (current.adjusted_close / previous.adjusted_close).ln()
            for previous, current in zip(observations, observations[1:])
        )
        count = len(returns)
        mean = sum(returns, start=ZERO) / Decimal(count)
        sample_variance = sum(
            ((item - mean) ** 2 for item in returns), start=ZERO
        ) / Decimal(count - 1)
        sample_stddev = sample_variance.sqrt()
        expected_move = sample_stddev * Decimal(event_horizon_trading_days).sqrt()
    return RealizedVolatilityExpectedMoveProxy(
        basis_as_of_date=basis_as_of_date or observations[-1].trading_date,
        staleness_trading_days=staleness_trading_days,
        refresh_required=refresh_required,
        daily_log_return_count=count,
        sample_daily_log_return_stddev=sample_stddev,
        event_horizon_trading_days=event_horizon_trading_days,
        expected_move_pct=expected_move,
        source=source.strip(),
    )
