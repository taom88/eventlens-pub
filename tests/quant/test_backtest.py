from datetime import date
from decimal import Decimal

from eventlens.domain.events import EventTiming
from eventlens.providers.models import DailyBar, EarningsEventRecord, MarketHistory
from eventlens.quant.backtest import run_event_study


def test_event_study_is_deterministic_and_does_not_invent_recommendations() -> None:
    bars = tuple(
        DailyBar(trading_date=date(2026, 1, day), open=Decimal(str(100 + day)), high=Decimal(str(102 + day)), low=Decimal(str(99 + day)), close=Decimal(str(101 + day)), volume=100)
        for day in range(1, 12)
    )
    event = EarningsEventRecord(
        ticker="BETA", event_date=date(2026, 1, 1), fiscal_quarter="2025Q4",
        timing=EventTiming.AFTER_MARKET_CLOSE, source_event_id="BETA:2026-01-01:2025Q4",
    )
    report = run_event_study("BETA", (event,), MarketHistory(ticker="BETA", bars=bars))
    assert report.event_count == 1
    assert report.comparable_count == 1
    assert report.recommendation_backtest_status == "unavailable_without_locked_hypotheses"
    assert report.observations[0].next_open_return_pct is not None
    assert report.next_open_summary.statistics.sample_count == 1
    assert report.next_open_summary.statistics.missing_count == 0
    assert report.next_open_summary.bootstrap is None
    assert len(report.input_sha256) == 64
    assert report.input_sha256 == run_event_study(
        "BETA", (event,), MarketHistory(ticker="BETA", bars=bars)
    ).input_sha256
    assert report.input_sha256 != run_event_study(
        "BETA", (event,), MarketHistory(ticker="BETA", bars=bars), seed=8
    ).input_sha256


def test_event_study_marks_short_history_non_comparable() -> None:
    bars = tuple(
        DailyBar(trading_date=date(2026, 1, day), open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"), volume=100)
        for day in range(1, 5)
    )
    event = EarningsEventRecord(
        ticker="BETA", event_date=date(2026, 1, 1), timing=EventTiming.AFTER_MARKET_CLOSE,
        source_event_id="BETA:2026-01-01:unknown",
    )
    report = run_event_study("BETA", (event,), MarketHistory(ticker="BETA", bars=bars))
    assert report.comparable_count == 0
    assert report.observations[0].missing_reason


def test_unknown_timing_is_blocked_instead_of_assumed_amc() -> None:
    bars = tuple(DailyBar(trading_date=date(2026, 1, day), open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"), volume=100) for day in range(1, 8))
    event = EarningsEventRecord(ticker="BETA", event_date=date(2026, 1, 3), timing=EventTiming.UNKNOWN, source_event_id="BETA:2026-01-03:unknown")
    report = run_event_study("BETA", (event,), MarketHistory(ticker="BETA", bars=bars))
    assert report.comparable_count == 0
    assert "unknown" in (report.observations[0].missing_reason or "")


def test_during_market_uses_prior_close_and_event_session() -> None:
    bars = tuple(
        DailyBar(
            trading_date=date(2026, 1, day), open=Decimal(str(100 + day)),
            high=Decimal(str(102 + day)), low=Decimal(str(99 + day)),
            close=Decimal(str(101 + day)), volume=100,
        )
        for day in range(1, 9)
    )
    event = EarningsEventRecord(
        ticker="BETA", event_date=date(2026, 1, 3),
        timing=EventTiming.DURING_MARKET_HOURS,
        source_event_id="BETA:2026-01-03:2025Q4",
    )
    report = run_event_study("BETA", (event,), MarketHistory(ticker="BETA", bars=bars))
    observation = report.observations[0]
    assert observation.reference_trading_date == date(2026, 1, 2)
    assert observation.first_post_event_trading_date == date(2026, 1, 3)
    assert observation.t_plus_1_close_return_pct is not None


def test_metric_confidence_intervals_keep_t1_when_t5_is_missing() -> None:
    bars = tuple(
        DailyBar(
            trading_date=date(2026, 1, day), open=Decimal("100"),
            high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
            volume=100,
        )
        for day in range(1, 5)
    )
    events = tuple(
        EarningsEventRecord(
            ticker="BETA", event_date=date(2026, 1, 1),
            timing=EventTiming.AFTER_MARKET_CLOSE,
            source_event_id=f"BETA:2026-01-01:2025Q{i}",
        )
        for i in (1, 2)
    )
    report = run_event_study("BETA", events, MarketHistory(ticker="BETA", bars=bars))
    assert report.comparable_count == 0
    assert report.next_open_bootstrap is None
    assert report.t_plus_1_bootstrap is None
    assert report.t_plus_5_bootstrap is None


def test_event_study_requires_four_observations_for_bootstrap_ci() -> None:
    bars = tuple(
        DailyBar(
            trading_date=date(2026, 1, day), open=Decimal("100"),
            high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
            volume=100,
        )
        for day in range(1, 8)
    )
    events = tuple(
        EarningsEventRecord(
            ticker="BETA", event_date=date(2026, 1, 1),
            timing=EventTiming.AFTER_MARKET_CLOSE,
            source_event_id=f"BETA:2026-01-01:2025Q{i}",
        )
        for i in range(4)
    )
    report = run_event_study("BETA", events, MarketHistory(ticker="BETA", bars=bars))
    assert report.next_open_bootstrap is not None
    assert report.t_plus_1_bootstrap is not None
