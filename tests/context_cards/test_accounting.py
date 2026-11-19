from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AccountingAdjustment,
    AccountingBasis,
    ConsensusEps,
    EarningsQuality,
    EpsActuals,
    FiscalPeriod,
    GrowthBasis,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizationMapping,
    NormalizedMetric,
    context_card_json_schema,
    compute_normalized_surprise,
)


PERIOD = FiscalPeriod(fiscal_year=2026, fiscal_quarter=1)


def _eps(
    value: str | None,
    basis: AccountingBasis,
    *,
    metric: NormalizedMetric = NormalizedMetric.DILUTED_EPS,
    period: FiscalPeriod = PERIOD,
    unit: MetricUnit = MetricUnit.USD_PER_SHARE,
) -> MetricValue:
    return MetricValue(
        metric=metric,
        value=None if value is None else Decimal(value),
        unit=unit,
        fiscal_period=period,
        accounting_basis=basis,
        growth_basis=GrowthBasis.NOT_APPLICABLE,
        scope=MetricScope.CONSOLIDATED,
    )


def _consensus(value: str, basis: AccountingBasis) -> ConsensusEps:
    return ConsensusEps(
        estimate=_eps(value, basis, metric=NormalizedMetric.EPS),
        provider="alpha_vantage",
        receipt_refs=("receipt:alpha-vantage:eps-estimate",),
    )


def _openai_mapping() -> NormalizationMapping:
    gaap = _eps("5.16", AccountingBasis.GAAP)
    adjusted = _eps(
        "4.14",
        AccountingBasis.ADJUSTED,
        metric=NormalizedMetric.ADJUSTED_EPS,
    )
    adjustment = AccountingAdjustment(
        adjustment_id="openai-investment-effect",
        adjustment_type="investment_gain_loss",
        name="OpenAI investment impact",
        eps_effect=_eps("1.02", AccountingBasis.NOT_APPLICABLE),
        receipt_refs=("receipt:example systems:fy2026-q1-openai-impact",),
    )
    return NormalizationMapping(
        mapping_id="demo-fy2026-q1-gaap-to-adjusted",
        source=gaap,
        target=adjusted,
        adjustments=(adjustment,),
        receipt_refs=("receipt:example systems:fy2026-q1-reconciliation",),
    )


def test_unknown_basis_provider_surprise_is_preserved_but_normalized_is_blocked() -> None:
    result = compute_normalized_surprise(
        _consensus("3.92", AccountingBasis.UNKNOWN),
        _eps("5.16", AccountingBasis.GAAP),
        provider_reported_surprise="1.24",
    )

    assert result.provider_reported is not None
    assert result.provider_reported.value == Decimal("1.24")
    assert result.provider_reported.valid_arithmetic is True
    assert result.provider_reported.valid_economic_comparison == "unknown"
    assert result.normalized.value is None
    assert result.normalized.status == "blocked"
    assert result.normalized.reason == "estimate_basis_not_verified"
    assert result.blockers == ("estimate_basis_not_verified",)


def test_openai_effect_is_a_receipted_mapping_without_inventing_recurring_eps() -> None:
    mapping = _openai_mapping()
    quality = EarningsQuality(
        actuals=EpsActuals(
            gaap=mapping.source,
            adjusted=mapping.target,
            gaap_receipt_refs=("receipt:example systems:gaap-eps",),
            adjusted_receipt_refs=("receipt:example systems:adjusted-eps",),
        ),
        normalization_mappings=(mapping,),
    )

    assert mapping.source.value == Decimal("5.16")
    assert mapping.target.value == Decimal("4.14")
    assert mapping.adjustments[0].eps_effect.value == Decimal("1.02")
    assert mapping.adjustments[0].receipt_refs
    assert quality.recurring_eps is None
    assert quality.recurring_eps_status == "not_computed"


def test_explicit_same_basis_inputs_compute_normalized_surprise() -> None:
    result = compute_normalized_surprise(
        _consensus("3.92", AccountingBasis.ADJUSTED),
        _eps("4.14", AccountingBasis.ADJUSTED, metric=NormalizedMetric.ADJUSTED_EPS),
        provider_reported_surprise="0.22",
    )

    assert result.normalized.value == Decimal("0.22")
    assert result.normalized.status == "computed"
    assert result.blockers == ()
    assert result.provider_reported is not None
    assert result.provider_reported.valid_arithmetic is True
    assert result.provider_reported.valid_economic_comparison is True


def test_mismatched_basis_blocks_without_a_mapping() -> None:
    result = compute_normalized_surprise(
        _consensus("3.92", AccountingBasis.ADJUSTED),
        _eps("5.16", AccountingBasis.GAAP),
        provider_reported_surprise="1.24",
    )

    assert result.provider_reported is not None
    assert result.provider_reported.valid_arithmetic is True
    assert result.provider_reported.valid_economic_comparison is False
    assert result.normalized.value is None
    assert result.blockers == ("accounting_basis_mismatch",)


def test_receipted_mapping_can_bridge_actual_to_estimate_basis() -> None:
    mapping = _openai_mapping()
    result = compute_normalized_surprise(
        _consensus("3.92", AccountingBasis.ADJUSTED),
        mapping.source,
        provider_reported_surprise="1.24",
        normalization_mapping=mapping,
    )

    assert result.normalized.value == Decimal("0.22")
    assert result.normalization_mapping_id == mapping.mapping_id
    # Provider arithmetic remains the raw 5.16 - 3.92 comparison, not 4.14 - 3.92.
    assert result.provider_reported is not None
    assert result.provider_reported.value == Decimal("1.24")
    assert result.provider_reported.valid_arithmetic is True
    assert result.provider_reported.valid_economic_comparison is False


def test_adjustments_require_receipts_and_exact_period_unit_reconciliation() -> None:
    with pytest.raises(ValidationError, match="receipt_refs"):
        AccountingAdjustment(
            adjustment_id="openai-investment-effect",
            adjustment_type="investment_gain_loss",
            name="OpenAI investment impact",
            eps_effect=_eps("1.02", AccountingBasis.NOT_APPLICABLE),
            receipt_refs=(),
        )

    mapping = _openai_mapping()
    incompatible = mapping.adjustments[0].model_copy(
        update={
            "eps_effect": _eps(
                "1.02",
                AccountingBasis.NOT_APPLICABLE,
                period=FiscalPeriod(fiscal_year=2026, fiscal_quarter=2),
            )
        }
    )
    with pytest.raises(ValidationError, match="fiscal period is incompatible"):
        NormalizationMapping(
            mapping_id="bad-period",
            source=mapping.source,
            target=mapping.target,
            adjustments=(incompatible,),
            receipt_refs=("receipt:example systems:reconciliation",),
        )


def test_unreconciled_mapping_and_unreceipted_recurring_eps_are_rejected() -> None:
    mapping = _openai_mapping()
    with pytest.raises(ValidationError, match="do not reconcile"):
        NormalizationMapping(
            mapping_id="bad-arithmetic",
            source=mapping.source,
            target=_eps(
                "4.15",
                AccountingBasis.ADJUSTED,
                metric=NormalizedMetric.ADJUSTED_EPS,
            ),
            adjustments=mapping.adjustments,
            receipt_refs=("receipt:example systems:reconciliation",),
        )

    with pytest.raises(ValidationError, match="methodology and receipt refs"):
        EarningsQuality(
            actuals=EpsActuals(
                gaap=mapping.source,
                gaap_receipt_refs=("receipt:example systems:gaap-eps",),
            ),
            recurring_eps=_eps("4.00", AccountingBasis.ADJUSTED),
            recurring_eps_status="computed",
        )


def test_earnings_quality_rejects_mapping_for_undeclared_actuals() -> None:
    mapping = _openai_mapping()

    with pytest.raises(ValidationError, match="must be declared EPS actuals"):
        EarningsQuality(
            actuals=EpsActuals(
                gaap=mapping.source,
                gaap_receipt_refs=("receipt:example systems:gaap-eps",),
            ),
            normalization_mappings=(mapping,),
        )


def test_accounting_contract_is_exposed_in_canonical_card_schema() -> None:
    definitions = context_card_json_schema()["$defs"]

    for name in (
        "AccountingAdjustment",
        "ConsensusEps",
        "EarningsQuality",
        "EpsActuals",
        "NormalizationMapping",
        "SurpriseComparison",
    ):
        assert name in definitions
