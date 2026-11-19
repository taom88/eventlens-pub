"""Optional benchmark-relative and transaction-cost analytics."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .backtest import EventStudyReport, event_study_report_sha256
from .event_study import apply_transaction_costs, beta_regression
from .models import (
    BetaRegressionResult,
    DescriptiveStatistics,
    Market,
    MarketAssumptions,
    TransactionCostResult,
)


class BenchmarkEventObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    event_date: date
    t_plus_1_return_pct: Decimal | None = None
    t_plus_5_return_pct: Decimal | None = None
    missing_reason: str | None = None


class CostScenarioPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market_assumptions: MarketAssumptions
    turnover: Decimal = Field(ge=0)
    policy_version: str = Field(min_length=1)
    analyst_override: bool = False


class BenchmarkRelativeEventMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_id: str = Field(min_length=1)
    benchmark_market: Market
    paired_t_plus_1_count: int = Field(ge=0)
    paired_t_plus_5_count: int = Field(ge=0)
    missing_pair_count: int = Field(ge=0)
    t_plus_1_excess_summary: DescriptiveStatistics
    t_plus_5_excess_summary: DescriptiveStatistics
    t_plus_1_beta: BetaRegressionResult
    t_plus_5_beta: BetaRegressionResult


class EventStudySupplement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    benchmark: BenchmarkRelativeEventMetrics
    t_plus_1_costs: TransactionCostResult
    t_plus_5_costs: TransactionCostResult
    cost_policy_version: str = Field(min_length=1)
    warnings: tuple[str, ...] = ()
    execution_status: str = "analysis_only"

    @model_validator(mode="after")
    def cannot_be_execution_instruction(self) -> "EventStudySupplement":
        if self.execution_status != "analysis_only":
            raise ValueError("event-study supplement cannot carry an execution status")
        return self


def _summary(values: tuple[Decimal, ...], missing_count: int) -> DescriptiveStatistics:
    ordered = tuple(sorted(values))
    count = len(ordered)
    if not count:
        return DescriptiveStatistics(
            sample_count=0, missing_count=missing_count,
            positive_count=0, negative_count=0, zero_count=0,
        )
    mean = sum(ordered, Decimal("0")) / Decimal(count)
    midpoint = count // 2
    median = ordered[midpoint] if count % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")
    variance = sum((item - mean) ** 2 for item in ordered) / Decimal(count - 1) if count > 1 else None
    positive = sum(item > 0 for item in ordered)
    negative = sum(item < 0 for item in ordered)
    return DescriptiveStatistics(
        sample_count=count,
        missing_count=missing_count,
        mean_pct=mean,
        median_pct=median,
        sample_stddev_pct=variance.sqrt() if variance is not None else None,
        min_pct=ordered[0],
        max_pct=ordered[-1],
        positive_count=positive,
        negative_count=negative,
        zero_count=count - positive - negative,
        positive_rate=Decimal(positive) / Decimal(count),
    )


def build_event_study_supplement(
    report: EventStudyReport,
    benchmark_observations: tuple[BenchmarkEventObservation, ...],
    *,
    benchmark_id: str,
    benchmark_market: Market,
    cost_policy: CostScenarioPolicy,
    minimum_beta_samples: int = 4,
) -> EventStudySupplement:
    """Build benchmark/cost context without mutating canonical event metrics."""

    if minimum_beta_samples < 4:
        raise ValueError("minimum_beta_samples must be at least four")
    benchmark_by_id = {item.event_id: item for item in benchmark_observations}
    if len(benchmark_by_id) != len(benchmark_observations):
        raise ValueError("benchmark observations must have unique event IDs")
    report_by_id = {item.event_id: item for item in report.observations}
    if any(item.event_id not in report_by_id for item in benchmark_observations):
        raise ValueError("benchmark observation does not match a base report event")

    t1_security: list[Decimal] = []
    t1_benchmark: list[Decimal] = []
    t5_security: list[Decimal] = []
    t5_benchmark: list[Decimal] = []
    t1_excess: list[Decimal] = []
    t5_excess: list[Decimal] = []
    missing_pairs = 0
    for event_id, observation in report_by_id.items():
        benchmark = benchmark_by_id.get(event_id)
        if benchmark is None:
            missing_pairs += 1
            continue
        if benchmark.event_date != observation.event_date:
            raise ValueError(f"benchmark event date mismatch for {event_id}")
        security_t1 = observation.t_plus_1_close_return_pct
        if security_t1 is not None and benchmark.t_plus_1_return_pct is not None:
            t1_security.append(security_t1)
            t1_benchmark.append(benchmark.t_plus_1_return_pct)
            t1_excess.append(security_t1 - benchmark.t_plus_1_return_pct)
        elif security_t1 is not None or benchmark.t_plus_1_return_pct is not None:
            missing_pairs += 1
        security_t5 = observation.t_plus_5_close_return_pct
        if security_t5 is not None and benchmark.t_plus_5_return_pct is not None:
            t5_security.append(security_t5)
            t5_benchmark.append(benchmark.t_plus_5_return_pct)
            t5_excess.append(security_t5 - benchmark.t_plus_5_return_pct)
        elif security_t5 is not None or benchmark.t_plus_5_return_pct is not None:
            missing_pairs += 1

    t1_beta = beta_regression(
        tuple(t1_security), tuple(t1_benchmark), benchmark_id=benchmark_id,
        benchmark_market=benchmark_market, minimum_samples=minimum_beta_samples,
    )
    t5_beta = beta_regression(
        tuple(t5_security), tuple(t5_benchmark), benchmark_id=benchmark_id,
        benchmark_market=benchmark_market, minimum_samples=minimum_beta_samples,
    )
    base_sha = event_study_report_sha256(report)
    policy_payload = cost_policy.model_dump(mode="json")
    input_sha = hashlib.sha256(json.dumps({
        "base_report_sha256": base_sha,
        "benchmark_id": benchmark_id,
        "benchmark_market": benchmark_market.value,
        "benchmark_observations": [
            item.model_dump(mode="json")
            for item in sorted(benchmark_observations, key=lambda value: (value.event_id, value.event_date))
        ],
        "cost_policy": policy_payload,
        "minimum_beta_samples": minimum_beta_samples,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    warnings: list[str] = []
    if len(t1_security) < minimum_beta_samples:
        warnings.append("T+1 beta unavailable: fewer than four paired observations")
    if len(t5_security) < minimum_beta_samples:
        warnings.append("T+5 beta unavailable: fewer than four paired observations")
    t1_mean = _summary(tuple(t1_excess), 0).mean_pct or Decimal("0")
    t5_mean = _summary(tuple(t5_excess), 0).mean_pct or Decimal("0")
    return EventStudySupplement(
        base_report_sha256=base_sha,
        input_sha256=input_sha,
        benchmark=BenchmarkRelativeEventMetrics(
            benchmark_id=benchmark_id,
            benchmark_market=benchmark_market,
            paired_t_plus_1_count=len(t1_security),
            paired_t_plus_5_count=len(t5_security),
            missing_pair_count=missing_pairs,
            t_plus_1_excess_summary=_summary(tuple(t1_excess), 0),
            t_plus_5_excess_summary=_summary(tuple(t5_excess), 0),
            t_plus_1_beta=t1_beta,
            t_plus_5_beta=t5_beta,
        ),
        t_plus_1_costs=apply_transaction_costs(t1_mean, cost_policy.turnover, cost_policy.market_assumptions),
        t_plus_5_costs=apply_transaction_costs(t5_mean, cost_policy.turnover, cost_policy.market_assumptions),
        cost_policy_version=cost_policy.policy_version,
        warnings=tuple(warnings),
    )


__all__ = [
    "BenchmarkEventObservation",
    "BenchmarkRelativeEventMetrics",
    "CostScenarioPolicy",
    "EventStudySupplement",
    "build_event_study_supplement",
]
