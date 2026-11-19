"""Ticker-agnostic historical earnings reaction study."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from eventlens.domain.events import EventTiming
from eventlens.domain.events import EarningsEvent, EventTimingResolutionError, resolve_event_window
from eventlens.domain.returns import PriceObservation, calculate_realized_moves
from eventlens.domain._decimal import canonical_decimal_context
from eventlens.providers.models import EarningsEventRecord, MarketHistory

from .event_study import bootstrap_mean_confidence_interval
from .models import BootstrapConfidenceInterval, DescriptiveStatistics, EventStudyMetricSummary


class EventStudyObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    event_date: date
    fiscal_quarter: str | None = None
    timing: EventTiming
    information_cutoff_at: datetime
    reference_trading_date: date | None = None
    first_post_event_trading_date: date | None = None
    next_open_return_pct: Decimal | None = None
    t_plus_1_close_return_pct: Decimal | None = None
    t_plus_5_close_return_pct: Decimal | None = None
    comparable: bool
    missing_reason: str | None = None


class EventStudyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticker: str
    event_count: int = Field(ge=0)
    comparable_count: int = Field(ge=0)
    observations: tuple[EventStudyObservation, ...]
    calculation_version: str = "event-study-v2"
    calculation_method: str = "canonical_event_window_realized_moves"
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    minimum_ci_observations: int = Field(ge=4)
    next_open_summary: EventStudyMetricSummary
    t_plus_1_summary: EventStudyMetricSummary
    t_plus_5_summary: EventStudyMetricSummary
    recommendation_backtest_status: str = "unavailable_without_locked_hypotheses"

    @property
    def next_open_bootstrap(self) -> BootstrapConfidenceInterval | None:
        return self.next_open_summary.bootstrap

    @property
    def t_plus_1_bootstrap(self) -> BootstrapConfidenceInterval | None:
        return self.t_plus_1_summary.bootstrap

    @property
    def t_plus_5_bootstrap(self) -> BootstrapConfidenceInterval | None:
        return self.t_plus_5_summary.bootstrap


def canonical_event_study_bytes(report: EventStudyReport) -> bytes:
    """Serialize a report with nulls retained for auditable missingness."""

    payload = report.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def event_study_report_sha256(report: EventStudyReport) -> str:
    return hashlib.sha256(canonical_event_study_bytes(report)).hexdigest()


def run_event_study(
    ticker: str,
    events: Iterable[EarningsEventRecord],
    history: MarketHistory,
    *,
    seed: int = 20260929,
    unknown_timing_convention: EventTiming | None = None,
    minimum_ci_observations: int = 4,
) -> EventStudyReport:
    """Run the event study using the canonical event-window calculations.

    The event date must be present in ``history`` because the canonical
    resolver needs an explicit ordered exchange-session set.  Unknown timing
    is blocked unless the analyst supplies an explicit BMO/AMC convention.
    """
    if minimum_ci_observations < 4:
        raise ValueError("minimum_ci_observations must be at least four")
    events = tuple(events)
    bars = tuple(history.bars)
    if history.ticker.upper() != ticker.upper():
        raise ValueError("market history ticker does not match event-study ticker")
    trading_dates = tuple(bar.trading_date for bar in bars)
    observations_by_date = {
        bar.trading_date: PriceObservation(
            trading_date=bar.trading_date,
            close=bar.close,
            open=bar.open,
        )
        for bar in bars
    }
    observations: list[EventStudyObservation] = []
    for event in events:
        cutoff = event.event_release_at or datetime.combine(event.event_date, time(20), tzinfo=timezone.utc)
        try:
            resolution = resolve_event_window(
                EarningsEvent(
                    ticker=event.ticker,
                    event_date=event.event_date,
                    timing=event.timing,
                    fiscal_quarter=event.fiscal_quarter,
                ),
                trading_dates,
                analyst_confirmed_unknown_timing_as=unknown_timing_convention,
            )
            reference = observations_by_date[resolution.reference_trading_date]
            post = tuple(
                observations_by_date[item]
                for item in resolution.resolved_post_event_trading_dates
                if item in observations_by_date
            )
            realized = calculate_realized_moves(reference, post, event_window=resolution)
        except (EventTimingResolutionError, KeyError, ValueError) as error:
            observations.append(
                EventStudyObservation(
                    event_id=event.source_event_id,
                    event_date=event.event_date,
                    fiscal_quarter=event.fiscal_quarter,
                    timing=event.timing,
                    information_cutoff_at=cutoff,
                    comparable=False,
                    missing_reason=str(error),
                )
            )
            continue

        first = observations_by_date[resolution.first_post_event_trading_date]
        with_decimal = lambda end, start: (end / start) - Decimal("1")
        next_open = with_decimal(first.open, reference.close) if first.open is not None else None
        observations.append(EventStudyObservation(
            event_id=event.source_event_id, event_date=event.event_date, fiscal_quarter=event.fiscal_quarter,
            timing=event.timing, information_cutoff_at=cutoff, reference_trading_date=realized.reference_trading_date,
            first_post_event_trading_date=realized.first_post_event_trading_date,
            next_open_return_pct=next_open,
            t_plus_1_close_return_pct=(realized.one_day.signed_return_pct if realized.one_day else None),
            t_plus_5_close_return_pct=(realized.five_day.signed_return_pct if realized.five_day else None),
            comparable=realized.five_day is not None,
            missing_reason=None if realized.five_day is not None else "fewer than five post-event sessions",
        ))
    comparable = tuple(item for item in observations if item.comparable)

    def metric_summary(field: str, metric_name: str) -> EventStudyMetricSummary:
        values = tuple(
            value for item in observations
            if (value := getattr(item, field)) is not None
        )
        missing_count = len(observations) - len(values)
        ordered = tuple(sorted(values))
        sample_count = len(ordered)
        if sample_count:
            with canonical_decimal_context():
                mean = sum(ordered, Decimal("0")) / Decimal(sample_count)
                midpoint = sample_count // 2
                median = ordered[midpoint] if sample_count % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")
                variance = (
                    sum((value - mean) ** 2 for value in ordered) / Decimal(sample_count - 1)
                    if sample_count > 1 else None
                )
                stddev = variance.sqrt() if variance is not None else None
                positive = sum(value > 0 for value in ordered)
                negative = sum(value < 0 for value in ordered)
                zero = sample_count - positive - negative
                positive_rate = Decimal(positive) / Decimal(sample_count)
            statistics = DescriptiveStatistics(
                sample_count=sample_count, missing_count=missing_count,
                mean_pct=mean, median_pct=median, sample_stddev_pct=stddev,
                min_pct=ordered[0], max_pct=ordered[-1], positive_count=positive,
                negative_count=negative, zero_count=zero, positive_rate=positive_rate,
            )
        else:
            statistics = DescriptiveStatistics(
                sample_count=0, missing_count=missing_count,
                positive_count=0, negative_count=0, zero_count=0,
            )
        bootstrap = (
            bootstrap_mean_confidence_interval(values, seed=seed)
            if sample_count >= minimum_ci_observations else None
        )
        return EventStudyMetricSummary(metric=metric_name, statistics=statistics, bootstrap=bootstrap)

    input_payload = {
        "ticker": ticker.upper(),
        "events": [item.model_dump(mode="json") for item in events],
        "history": history.model_dump(mode="json"),
        "unknown_timing_convention": unknown_timing_convention.value if unknown_timing_convention else None,
        "seed": seed,
        "minimum_ci_observations": minimum_ci_observations,
    }
    input_sha256 = hashlib.sha256(
        json.dumps(input_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return EventStudyReport(
        ticker=ticker.upper(), event_count=len(observations), comparable_count=len(comparable),
        observations=tuple(observations), input_sha256=input_sha256,
        minimum_ci_observations=minimum_ci_observations,
        next_open_summary=metric_summary("next_open_return_pct", "next_open_return_pct"),
        t_plus_1_summary=metric_summary("t_plus_1_close_return_pct", "t_plus_1_close_return_pct"),
        t_plus_5_summary=metric_summary("t_plus_5_close_return_pct", "t_plus_5_close_return_pct"),
    )


__all__ = [
    "EventStudyObservation",
    "EventStudyReport",
    "canonical_event_study_bytes",
    "event_study_report_sha256",
    "run_event_study",
]
