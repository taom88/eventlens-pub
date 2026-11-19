"""Canonical deterministic event analytics for EventLens."""

from .comparison import (
    ExpectedVsRealizedComparison,
    RealizationStatus,
    compare_expected_and_realized,
)
from .events import (
    EarningsEvent,
    EventTiming,
    EventTimingResolutionError,
    EventWindowResolution,
    resolve_event_window,
)
from .returns import (
    PriceObservation,
    RealizedMoveAnalysis,
    RealizedReturn,
    calculate_realized_moves,
)
from .volatility import (
    ATMStraddleImpliedMove,
    ATMStraddleSelection,
    DailyClose,
    DailyPriceBasis,
    ExpectationMeasureKind,
    OptionChainSnapshot,
    OptionQuote,
    OptionSelectionError,
    OptionSnapshotKind,
    OptionType,
    PreEventSnapshotAlignment,
    RealizedVolatilityExpectedMoveProxy,
    calculate_atm_straddle_implied_move,
    calculate_realized_volatility_expected_move_proxy,
    select_atm_straddle,
    select_last_pre_event_snapshot,
)

__all__ = [
    "ATMStraddleImpliedMove",
    "ATMStraddleSelection",
    "DailyClose",
    "DailyPriceBasis",
    "EarningsEvent",
    "EventTiming",
    "EventTimingResolutionError",
    "EventWindowResolution",
    "ExpectationMeasureKind",
    "ExpectedVsRealizedComparison",
    "OptionChainSnapshot",
    "OptionQuote",
    "OptionSelectionError",
    "OptionSnapshotKind",
    "OptionType",
    "PreEventSnapshotAlignment",
    "PriceObservation",
    "RealizationStatus",
    "RealizedMoveAnalysis",
    "RealizedReturn",
    "RealizedVolatilityExpectedMoveProxy",
    "calculate_atm_straddle_implied_move",
    "calculate_realized_moves",
    "calculate_realized_volatility_expected_move_proxy",
    "compare_expected_and_realized",
    "resolve_event_window",
    "select_atm_straddle",
    "select_last_pre_event_snapshot",
]
