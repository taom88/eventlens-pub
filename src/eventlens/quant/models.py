"""Event-conditioned quantitative contracts owned and validated by EventLens."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Market(StrEnum):
    US = "US"
    HONG_KONG = "HK"


class MarketAssumptions(BaseModel):
    """Explicit, deliberately conservative cost assumptions by market."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: Market
    currency: str = Field(min_length=3, max_length=3)
    exchange_calendar: str = Field(min_length=1)
    exchange_timezone: str = Field(min_length=1)
    commission_bps: Decimal = Field(ge=0)
    fees_bps: Decimal = Field(ge=0)
    bid_ask_spread_bps: Decimal = Field(ge=0)
    slippage_bps: Decimal = Field(ge=0)
    market_impact_caveat: str = Field(min_length=1)


US_MARKET_ASSUMPTIONS = MarketAssumptions(
    market=Market.US,
    currency="USD",
    exchange_calendar="XNYS",
    exchange_timezone="America/New_York",
    commission_bps=Decimal("1"),
    fees_bps=Decimal("1"),
    bid_ask_spread_bps=Decimal("5"),
    slippage_bps=Decimal("3"),
    market_impact_caveat="Market impact is not estimated; costs may be materially higher for illiquid names.",
)
HK_MARKET_ASSUMPTIONS = MarketAssumptions(
    market=Market.HONG_KONG,
    currency="HKD",
    exchange_calendar="XHKG",
    exchange_timezone="Asia/Hong_Kong",
    commission_bps=Decimal("3"),
    fees_bps=Decimal("6"),
    bid_ask_spread_bps=Decimal("8"),
    slippage_bps=Decimal("5"),
    market_impact_caveat="Market impact is not estimated; costs may be materially higher for illiquid names.",
)


class EventCohortObservation(BaseModel):
    """A point-in-time event observation suitable for a conditional study."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    event_date: date
    market: Market = Market.US
    exchange_calendar: str = "XNYS"
    feature_available_at: datetime | None = None
    information_cutoff_at: datetime
    realized_return_pct: Decimal | None = None
    benchmark_return_pct: Decimal | None = None
    security_active_at_event: bool = True
    missing_observation_reason: str | None = None

    @field_validator("feature_available_at", "information_cutoff_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cohort timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def feature_is_point_in_time_and_security_is_not_survivorship_filtered(self) -> "EventCohortObservation":
        assumptions = US_MARKET_ASSUMPTIONS if self.market is Market.US else HK_MARKET_ASSUMPTIONS
        if self.exchange_calendar != assumptions.exchange_calendar:
            raise ValueError("cohort exchange calendar does not match its market")
        if self.feature_available_at is not None and self.feature_available_at > self.information_cutoff_at:
            raise ValueError("post-cutoff feature is prohibited (post-event leakage)")
        incomplete = (
            self.feature_available_at is None
            or self.realized_return_pct is None
            or not self.security_active_at_event
        )
        if incomplete and not self.missing_observation_reason:
            raise ValueError("missing or inactive cohort observations require an explicit reason")
        if not incomplete and self.missing_observation_reason is not None:
            raise ValueError("complete cohort observations cannot carry a missing-observation reason")
        return self

    @property
    def is_comparable(self) -> bool:
        return (
            self.security_active_at_event
            and self.feature_available_at is not None
            and self.realized_return_pct is not None
        )


class TransactionCostResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gross_return_pct: Decimal
    turnover: Decimal = Field(ge=0)
    turnover_convention: str = "one_way_notional_traded_including_each_entry_and_exit_leg"
    entry_exit_legs: int = Field(ge=0)
    one_way_cost_bps: Decimal = Field(ge=0)
    total_cost_pct: Decimal = Field(ge=0)
    net_return_pct: Decimal
    market_impact_caveat: str = Field(min_length=1)


class BootstrapConfidenceInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    resamples: int = Field(ge=100)
    sample_count: int = Field(ge=2)
    missing_observation_count: int = Field(ge=0)
    statistic: str = "mean"
    sampling_method: str = "iid_nonparametric_percentile_bootstrap_point_estimate_inclusive"
    estimate_pct: Decimal
    lower_pct: Decimal
    upper_pct: Decimal
    confidence_level: Decimal = Field(gt=0, lt=1)
    sensitivity: tuple["BootstrapSensitivity", ...] = ()

    @model_validator(mode="after")
    def confidence_interval_is_ordered(self) -> "BootstrapConfidenceInterval":
        if not self.lower_pct <= self.estimate_pct <= self.upper_pct:
            raise ValueError("estimate must lie within bootstrap confidence interval")
        return self


class BootstrapSensitivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resamples: int = Field(ge=100)
    lower_pct: Decimal
    upper_pct: Decimal


class DescriptiveStatistics(BaseModel):
    """Typed, non-inferential summary for one event-study metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    mean_pct: Decimal | None = None
    median_pct: Decimal | None = None
    sample_stddev_pct: Decimal | None = None
    min_pct: Decimal | None = None
    max_pct: Decimal | None = None
    positive_count: int = Field(ge=0)
    negative_count: int = Field(ge=0)
    zero_count: int = Field(ge=0)
    positive_rate: Decimal | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def counts_and_values_are_coherent(self) -> "DescriptiveStatistics":
        if self.positive_count + self.negative_count + self.zero_count != self.sample_count:
            raise ValueError("direction counts must sum to sample_count")
        if self.sample_count == 0:
            if any(value is not None for value in (self.mean_pct, self.median_pct, self.sample_stddev_pct, self.min_pct, self.max_pct, self.positive_rate)):
                raise ValueError("empty statistics cannot contain numeric summaries")
        else:
            required = (self.mean_pct, self.median_pct, self.min_pct, self.max_pct, self.positive_rate)
            if any(value is None for value in required):
                raise ValueError("non-empty statistics require complete location and rate summaries")
            if self.sample_count >= 2 and self.sample_stddev_pct is None:
                raise ValueError("sample_stddev_pct is required for at least two observations")
            if self.sample_count == 1 and self.sample_stddev_pct is not None:
                raise ValueError("sample_stddev_pct must be null for one observation")
            if not self.min_pct <= self.median_pct <= self.max_pct:
                raise ValueError("median must lie between min and max")
            if not self.min_pct <= self.mean_pct <= self.max_pct:
                raise ValueError("mean must lie between min and max")
            if self.positive_rate != Decimal(self.positive_count) / Decimal(self.sample_count):
                raise ValueError("positive_rate must equal positive_count / sample_count")
        return self


class EventStudyMetricSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str = Field(min_length=1)
    statistics: DescriptiveStatistics
    bootstrap: BootstrapConfidenceInterval | None = None


class BetaRegressionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: int = Field(ge=0)
    input_pair_count: int = Field(ge=0)
    missing_pair_count: int = Field(ge=0)
    minimum_sample_count: int = Field(ge=3)
    benchmark_id: str = Field(min_length=1)
    benchmark_market: Market
    benchmark_exchange_calendar: str = Field(min_length=1)
    alpha_pct: Decimal | None = None
    beta: Decimal | None = None
    r_squared: Decimal | None = Field(default=None, ge=0, le=1)
    warning: str | None = None


class ScenarioAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1)
    scenario_returns_pct: tuple[Decimal, ...] = Field(min_length=1)
    current_weight: Decimal = Field(ge=0, le=1)
    max_weight: Decimal = Field(gt=0, le=1)
    liquidity_limit: Decimal = Field(gt=0, le=1)

    @model_validator(mode="after")
    def max_weight_respects_liquidity(self) -> "ScenarioAsset":
        if self.max_weight > self.liquidity_limit:
            raise ValueError("max_weight cannot exceed the stated liquidity limit")
        return self


class ScenarioOptimizationResult(BaseModel):
    """Scenario-analysis output only; it is never an executable order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = "greedy_worst_asset_ranking_heuristic_not_global_maximin"
    weights: tuple[tuple[str, Decimal], ...] = Field(min_length=1)
    expected_worst_case_return_pct: Decimal
    turnover: Decimal = Field(ge=0)
    max_turnover: Decimal = Field(ge=0)
    concentration_limit: Decimal = Field(gt=0, le=1)
    cash_weight: Decimal = Field(ge=0, le=1)
    cash_convention: str = "unallocated long-only weight is held as zero-return cash"
    scenario_only_notice: str = "Scenario analysis only; no broker execution is supported."

    @model_validator(mode="after")
    def weights_satisfy_declared_constraints(self) -> "ScenarioOptimizationResult":
        values = [weight for _, weight in self.weights]
        if len({asset_id for asset_id, _ in self.weights}) != len(self.weights):
            raise ValueError("optimization weights need unique asset IDs")
        if any(weight < 0 or weight > self.concentration_limit for weight in values):
            raise ValueError("weights violate long-only or concentration constraint")
        total = sum(values, Decimal("0"))
        if total > Decimal("1"):
            raise ValueError("long-only scenario weights cannot exceed 100%")
        if total + self.cash_weight != Decimal("1"):
            raise ValueError("cash_weight must make long-only weights sum to 100%")
        if self.turnover > self.max_turnover:
            raise ValueError("optimization turnover exceeds its constraint")
        return self


class AlphaZooAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_signal_id: str = Field(min_length=1)
    tested_candidates: int = Field(ge=1)
    raw_p_value: Decimal | None = Field(default=None, ge=0, le=1)
    adjusted_p_value: Decimal | None = Field(default=None, ge=0, le=1)
    upstream_integration_status: str = "blocked_unverified_upstream_api"
    exploratory_warning: str = Field(min_length=1)
