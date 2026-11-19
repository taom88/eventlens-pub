from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AccountingBasis,
    FiscalPeriod,
    GrowthBasis,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
    convert_scale,
)


def _metric(**changes: object) -> MetricValue:
    payload: dict[str, object] = {
        "metric": NormalizedMetric.REVENUE,
        "value": Decimal("77.673"),
        "unit": MetricUnit.USD_BILLION,
        "fiscal_period": FiscalPeriod(fiscal_year=2026, fiscal_quarter=1),
        "accounting_basis": AccountingBasis.GAAP,
        "growth_basis": GrowthBasis.NOT_APPLICABLE,
        "scope": MetricScope.CONSOLIDATED,
    }
    payload.update(changes)
    return MetricValue(**payload)


def test_usd_million_and_billion_scale_conversion_preserves_decimal_precision() -> None:
    assert convert_scale(Decimal("77673.125"), MetricUnit.USD_MILLION, MetricUnit.USD_BILLION) == Decimal(
        "77.673125"
    )
    assert convert_scale(Decimal("77.673125"), MetricUnit.USD_BILLION, MetricUnit.USD_MILLION) == Decimal(
        "77673.125"
    )


def test_ratio_percent_and_percentage_points_have_distinct_display_semantics() -> None:
    assert _metric(value=Decimal("0.316327"), unit=MetricUnit.RATIO).display_value() == "31.632700%"
    assert _metric(value=Decimal("31.6327"), unit=MetricUnit.PERCENT).display_value() == "31.6327%"
    assert _metric(value=Decimal("3.1"), unit=MetricUnit.PERCENTAGE_POINTS).display_value() == "3.1 pp"


def test_periodless_metrics_are_rejected() -> None:
    payload = _metric().model_dump()
    del payload["fiscal_period"]

    with pytest.raises(ValidationError, match="fiscal_period"):
        MetricValue(**payload)


def test_none_remains_none_when_scales_are_converted() -> None:
    assert convert_scale(None, MetricUnit.USD_MILLION, MetricUnit.USD_BILLION) is None


def test_provider_unknown_accounting_basis_is_preserved() -> None:
    metric = _metric(accounting_basis=AccountingBasis.UNKNOWN)

    assert metric.accounting_basis is AccountingBasis.UNKNOWN
    assert metric.model_dump(mode="json")["accounting_basis"] == "unknown"


@pytest.mark.parametrize(
    ("from_unit", "to_unit"),
    [
        (MetricUnit.RATIO, MetricUnit.PERCENT),
        (MetricUnit.PERCENT, MetricUnit.PERCENTAGE_POINTS),
        (MetricUnit.USD_MILLION, MetricUnit.USD_PER_SHARE),
    ],
)
def test_invalid_scale_conversions_are_rejected(from_unit: MetricUnit, to_unit: MetricUnit) -> None:
    with pytest.raises(ValueError, match="cannot convert scale"):
        convert_scale(Decimal("1"), from_unit, to_unit)
