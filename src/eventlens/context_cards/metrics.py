"""Canonical, non-inferential metric types for context cards.

This module deliberately represents units and analytical bases separately.  In
particular, a ratio is not a percent, percentage points are not either, and a
provider's unknown accounting basis remains unknown.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class NormalizedMetric(StrEnum):
    """Canonical generic financial metric names.

    Company-specific KPI names and provider aliases intentionally belong to a
    later ontology/extraction layer.
    """

    REVENUE = "revenue"
    GROSS_PROFIT = "gross_profit"
    OPERATING_INCOME = "operating_income"
    NET_INCOME = "net_income"
    EPS = "eps"
    DILUTED_EPS = "diluted_eps"
    ADJUSTED_EPS = "adjusted_eps"
    FREE_CASH_FLOW = "free_cash_flow"
    OPERATING_MARGIN = "operating_margin"
    GROWTH = "growth"
    GUIDANCE = "guidance"
    SHARE_COUNT = "share_count"


class MetricUnit(StrEnum):
    """Units are semantic, rather than merely display formatting."""

    USD = "USD"
    USD_MILLION = "USD_million"
    USD_BILLION = "USD_billion"
    USD_PER_SHARE = "USD_per_share"
    PERCENT = "percent"
    PERCENTAGE_POINTS = "percentage_points"
    RATIO = "ratio"
    COUNT = "count"


class AccountingBasis(StrEnum):
    GAAP = "gaap"
    ADJUSTED = "adjusted"
    NON_GAAP = "non_gaap"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class GrowthBasis(StrEnum):
    REPORTED = "reported"
    CONSTANT_CURRENCY = "constant_currency"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class MetricScope(StrEnum):
    CONSOLIDATED = "consolidated"
    SEGMENT = "segment"
    PRODUCT = "product"
    GEOGRAPHY = "geography"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class FiscalPeriod(BaseModel):
    """A fiscal period supplied with every numeric metric.

    A fiscal year alone explicitly denotes an annual metric; a quarter adds a
    fiscal-quarter identity.  ``period_end`` is optional source detail, not a
    substitute for a fiscal period.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    fiscal_year: Annotated[int, Field(ge=1)]
    fiscal_quarter: Annotated[int, Field(ge=1, le=4)] | None = None
    period_end: date | None = None

    @property
    def label(self) -> str:
        suffix = f"_Q{self.fiscal_quarter}" if self.fiscal_quarter is not None else ""
        return f"FY{self.fiscal_year}{suffix}"


class MetricValue(BaseModel):
    """A numeric metric with non-optional unit, period, bases, and scope."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    metric: NormalizedMetric
    value: FiniteDecimal | None
    unit: MetricUnit
    fiscal_period: FiscalPeriod
    accounting_basis: AccountingBasis
    growth_basis: GrowthBasis
    scope: MetricScope

    @model_validator(mode="after")
    def unit_matches_metric_semantics(self) -> "MetricValue":
        if self.metric == NormalizedMetric.GROWTH and self.unit not in {
            MetricUnit.PERCENT,
            MetricUnit.RATIO,
            MetricUnit.PERCENTAGE_POINTS,
        }:
            raise ValueError("growth metrics require ratio, percent, or percentage-points units")
        return self

    def display_value(self) -> str | None:
        """Return an unambiguous human-readable value without changing its unit."""
        if self.value is None:
            return None
        if self.unit is MetricUnit.RATIO:
            return f"{self.value * Decimal('100')}%"
        if self.unit is MetricUnit.PERCENT:
            return f"{self.value}%"
        if self.unit is MetricUnit.PERCENTAGE_POINTS:
            return f"{self.value} pp"
        return f"{self.value} {self.unit.value}"


def normalize_metric(value: str | NormalizedMetric) -> NormalizedMetric:
    """Normalize spelling/case of a canonical metric name, never provider aliases."""
    if isinstance(value, NormalizedMetric):
        return value
    if not isinstance(value, str):
        raise TypeError("metric name must be a string or NormalizedMetric")
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    try:
        return NormalizedMetric(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported canonical metric: {value!r}") from exc


_USD_SCALE_FACTORS: dict[MetricUnit, Decimal] = {
    MetricUnit.USD: Decimal("1"),
    MetricUnit.USD_MILLION: Decimal("1000000"),
    MetricUnit.USD_BILLION: Decimal("1000000000"),
}


def convert_scale(
    value: Decimal | int | str | None,
    from_unit: MetricUnit,
    to_unit: MetricUnit,
) -> Decimal | None:
    """Convert among USD amount scales only, preserving :class:`Decimal` precision.

    Ratios, percents, and percentage points have distinct meanings, so this
    function intentionally refuses to convert between them.
    """
    if value is None:
        return None
    if from_unit not in _USD_SCALE_FACTORS or to_unit not in _USD_SCALE_FACTORS:
        raise ValueError(f"cannot convert scale from {from_unit.value} to {to_unit.value}")
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return decimal_value * _USD_SCALE_FACTORS[from_unit] / _USD_SCALE_FACTORS[to_unit]
