"""Fail-closed EPS and accounting-normalization primitives.

Provider arithmetic is deliberately represented independently from an
economically comparable surprise.  A subtraction can be arithmetically right
while still comparing GAAP and non-GAAP numbers (or an unknown estimate basis).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .metrics import (
    AccountingBasis,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
)


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]

_EPS_METRICS = {
    NormalizedMetric.EPS,
    NormalizedMetric.DILUTED_EPS,
    NormalizedMetric.ADJUSTED_EPS,
}


class _AccountingModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


def _validate_eps_metric(value: MetricValue, *, field_name: str) -> None:
    if value.metric not in _EPS_METRICS:
        raise ValueError(f"{field_name} must be an EPS metric")
    if value.unit is not MetricUnit.USD_PER_SHARE:
        raise ValueError(f"{field_name} must use USD_per_share")


def _same_period(left: MetricValue, right: MetricValue) -> bool:
    return left.fiscal_period == right.fiscal_period


class EpsActuals(_AccountingModel):
    """Issuer EPS actuals, keeping GAAP and adjusted values distinct."""

    gaap: MetricValue | None = None
    adjusted: MetricValue | None = None
    gaap_receipt_refs: tuple[NonEmptyStr, ...] = ()
    adjusted_receipt_refs: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def actuals_are_typed_and_receipted(self) -> "EpsActuals":
        if self.gaap is None and self.adjusted is None:
            raise ValueError("at least one EPS actual is required")
        if self.gaap is not None:
            _validate_eps_metric(self.gaap, field_name="gaap")
            if self.gaap.accounting_basis is not AccountingBasis.GAAP:
                raise ValueError("gaap EPS actual must declare gaap accounting basis")
            if not self.gaap_receipt_refs:
                raise ValueError("gaap EPS actual requires receipt refs")
        elif self.gaap_receipt_refs:
            raise ValueError("gaap receipt refs require a gaap EPS actual")
        if self.adjusted is not None:
            _validate_eps_metric(self.adjusted, field_name="adjusted")
            if self.adjusted.accounting_basis not in {
                AccountingBasis.ADJUSTED,
                AccountingBasis.NON_GAAP,
            }:
                raise ValueError("adjusted EPS actual must declare adjusted or non_gaap basis")
            if not self.adjusted_receipt_refs:
                raise ValueError("adjusted EPS actual requires receipt refs")
        elif self.adjusted_receipt_refs:
            raise ValueError("adjusted receipt refs require an adjusted EPS actual")
        if self.gaap is not None and self.adjusted is not None:
            if not _same_period(self.gaap, self.adjusted):
                raise ValueError("GAAP and adjusted EPS actuals must use the same fiscal period")
            if self.gaap.unit is not self.adjusted.unit:
                raise ValueError("GAAP and adjusted EPS actuals must use compatible units")
            if self.gaap.scope is not self.adjusted.scope:
                raise ValueError("GAAP and adjusted EPS actuals must use the same scope")
        return self


class ConsensusEps(_AccountingModel):
    """One EPS estimate with its provider and declared accounting basis."""

    estimate: MetricValue
    provider: NonEmptyStr
    receipt_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def estimate_is_eps(self) -> "ConsensusEps":
        _validate_eps_metric(self.estimate, field_name="consensus estimate")
        return self


class AccountingAdjustment(_AccountingModel):
    """A signed EPS effect included in the source basis and removed at target.

    Positive effects raise source-basis EPS; negative effects lower it.  Thus a
    GAAP-to-adjusted mapping is ``adjusted = gaap - sum(eps_effect)``.
    """

    adjustment_id: NonEmptyStr
    adjustment_type: NonEmptyStr
    name: NonEmptyStr
    eps_effect: MetricValue
    receipt_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def effect_is_basis_neutral_eps(self) -> "AccountingAdjustment":
        _validate_eps_metric(self.eps_effect, field_name="adjustment eps_effect")
        if self.eps_effect.accounting_basis is not AccountingBasis.NOT_APPLICABLE:
            raise ValueError("adjustment eps_effect accounting basis must be not_applicable")
        return self


class NormalizationMapping(_AccountingModel):
    """Receipt-backed bridge between two explicit EPS accounting bases."""

    mapping_id: NonEmptyStr
    source: MetricValue
    target: MetricValue
    adjustments: tuple[AccountingAdjustment, ...] = Field(min_length=1)
    receipt_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)
    methodology: Literal["source_eps_minus_adjustments"] = "source_eps_minus_adjustments"

    @model_validator(mode="after")
    def mapping_reconciles_exactly(self) -> "NormalizationMapping":
        _validate_eps_metric(self.source, field_name="mapping source")
        _validate_eps_metric(self.target, field_name="mapping target")
        if self.source.value is None or self.target.value is None:
            raise ValueError("normalization mapping endpoints require known values")
        if self.source.accounting_basis in {
            AccountingBasis.UNKNOWN,
            AccountingBasis.NOT_APPLICABLE,
        } or self.target.accounting_basis in {
            AccountingBasis.UNKNOWN,
            AccountingBasis.NOT_APPLICABLE,
        }:
            raise ValueError("normalization mapping endpoints require explicit accounting bases")
        if self.source.accounting_basis is self.target.accounting_basis:
            raise ValueError("normalization mapping must bridge different accounting bases")
        if not _same_period(self.source, self.target):
            raise ValueError("normalization mapping endpoints must use the same fiscal period")
        if self.source.unit is not self.target.unit:
            raise ValueError("normalization mapping endpoints must use compatible units")
        if self.source.scope is not self.target.scope:
            raise ValueError("normalization mapping endpoints must use the same scope")

        total_effect = Decimal("0")
        for adjustment in self.adjustments:
            effect = adjustment.eps_effect
            if effect.value is None:
                raise ValueError("normalization adjustment requires a known EPS effect")
            if not _same_period(effect, self.source):
                raise ValueError("normalization adjustment fiscal period is incompatible")
            if effect.unit is not self.source.unit:
                raise ValueError("normalization adjustment unit is incompatible")
            if effect.scope is not self.source.scope:
                raise ValueError("normalization adjustment scope is incompatible")
            total_effect += effect.value

        if self.source.value - total_effect != self.target.value:
            raise ValueError("normalization adjustments do not reconcile source EPS to target EPS")
        return self


class EarningsQuality(_AccountingModel):
    """Accounting reconciliation without an automatically invented recurring EPS."""

    actuals: EpsActuals
    normalization_mappings: tuple[NormalizationMapping, ...] = ()
    recurring_eps: MetricValue | None = None
    recurring_eps_status: Literal["not_computed", "computed"] = "not_computed"
    recurring_eps_methodology: NonEmptyStr | None = None
    recurring_eps_receipt_refs: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def recurring_signal_fails_closed(self) -> "EarningsQuality":
        declared_actuals = {
            actual
            for actual in (self.actuals.gaap, self.actuals.adjusted)
            if actual is not None
        }
        for mapping in self.normalization_mappings:
            if mapping.source not in declared_actuals or mapping.target not in declared_actuals:
                raise ValueError("normalization mapping endpoints must be declared EPS actuals")
        if self.recurring_eps_status == "not_computed":
            if self.recurring_eps is not None:
                raise ValueError("not_computed recurring EPS must remain null")
            if self.recurring_eps_methodology is not None or self.recurring_eps_receipt_refs:
                raise ValueError("not_computed recurring EPS cannot carry methodology or receipts")
        else:
            if self.recurring_eps is None:
                raise ValueError("computed recurring EPS requires a value")
            _validate_eps_metric(self.recurring_eps, field_name="recurring_eps")
            if self.recurring_eps.accounting_basis in {
                AccountingBasis.UNKNOWN,
                AccountingBasis.NOT_APPLICABLE,
            }:
                raise ValueError("computed recurring EPS requires an explicit accounting basis")
            reference_actual = self.actuals.gaap or self.actuals.adjusted
            assert reference_actual is not None
            if _compatibility_blocker(self.recurring_eps, reference_actual) is not None:
                raise ValueError("computed recurring EPS is incompatible with declared actuals")
            if not self.recurring_eps_methodology or not self.recurring_eps_receipt_refs:
                raise ValueError("computed recurring EPS requires methodology and receipt refs")
        return self


class _ProviderSurprise(_AccountingModel):
    value: FiniteDecimal
    valid_arithmetic: bool
    valid_economic_comparison: bool | Literal["unknown"]


class _NormalizedSurprise(_AccountingModel):
    value: FiniteDecimal | None = None
    status: Literal["computed", "blocked"]
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def value_matches_status(self) -> "_NormalizedSurprise":
        if self.status == "computed" and self.value is None:
            raise ValueError("computed normalized surprise requires a value")
        if self.status == "computed" and self.reason is not None:
            raise ValueError("computed normalized surprise cannot carry a blocker reason")
        if self.status == "blocked" and (self.value is not None or self.reason is None):
            raise ValueError("blocked normalized surprise requires a reason and null value")
        return self


class SurpriseComparison(_AccountingModel):
    """Provider arithmetic and normalized economic surprise side by side."""

    estimate: MetricValue
    actual: MetricValue
    provider: NonEmptyStr
    provider_reported: _ProviderSurprise | None = None
    normalized: _NormalizedSurprise
    normalization_mapping_id: NonEmptyStr | None = None
    blockers: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def result_state_is_coherent(self) -> "SurpriseComparison":
        if self.normalized.status == "blocked" and not self.blockers:
            raise ValueError("blocked surprise comparison requires blockers")
        if self.normalized.status == "computed" and self.blockers:
            raise ValueError("computed surprise comparison cannot carry blockers")
        if self.normalization_mapping_id is not None and self.normalized.status != "computed":
            raise ValueError("blocked comparison cannot claim a normalization mapping")
        return self


def compute_normalized_surprise(
    consensus: ConsensusEps,
    actual: MetricValue,
    *,
    provider_reported_surprise: Decimal | int | str | None = None,
    normalization_mapping: NormalizationMapping | None = None,
) -> SurpriseComparison:
    """Compute ``actual - estimate`` only for economically comparable EPS.

    Provider-reported arithmetic is retained and checked even if economic
    normalization is blocked.  A mapping may bridge the actual to the estimate
    basis only when its exact source value, period, unit, and scope match.
    """

    estimate = consensus.estimate
    _validate_eps_metric(actual, field_name="actual")
    provider_value = (
        None
        if provider_reported_surprise is None
        else Decimal(str(provider_reported_surprise))
    )
    raw_difference = (
        None
        if estimate.value is None or actual.value is None
        else actual.value - estimate.value
    )

    provider_result: _ProviderSurprise | None = None
    if provider_value is not None:
        provider_result = _ProviderSurprise(
            value=provider_value,
            valid_arithmetic=raw_difference is not None and provider_value == raw_difference,
            valid_economic_comparison=(
                True
                if estimate.accounting_basis is actual.accounting_basis
                and estimate.accounting_basis
                not in {AccountingBasis.UNKNOWN, AccountingBasis.NOT_APPLICABLE}
                else "unknown"
                if AccountingBasis.UNKNOWN
                in {estimate.accounting_basis, actual.accounting_basis}
                else False
            ),
        )

    common_blocker = _compatibility_blocker(estimate, actual)
    if estimate.accounting_basis is AccountingBasis.UNKNOWN:
        return _blocked_comparison(
            estimate, actual, consensus.provider, provider_result, "estimate_basis_not_verified"
        )
    if actual.accounting_basis is AccountingBasis.UNKNOWN:
        return _blocked_comparison(
            estimate, actual, consensus.provider, provider_result, "actual_basis_not_verified"
        )
    if estimate.accounting_basis is AccountingBasis.NOT_APPLICABLE:
        return _blocked_comparison(
            estimate, actual, consensus.provider, provider_result, "estimate_basis_not_applicable"
        )
    if actual.accounting_basis is AccountingBasis.NOT_APPLICABLE:
        return _blocked_comparison(
            estimate, actual, consensus.provider, provider_result, "actual_basis_not_applicable"
        )
    if common_blocker is not None:
        return _blocked_comparison(
            estimate, actual, consensus.provider, provider_result, common_blocker
        )
    if estimate.value is None or actual.value is None:
        return _blocked_comparison(
            estimate, actual, consensus.provider, provider_result, "missing_eps_value"
        )

    normalized_actual = actual
    mapping_id: str | None = None
    if estimate.accounting_basis is not actual.accounting_basis:
        mapping_blocker = _mapping_blocker(normalization_mapping, actual, estimate)
        if mapping_blocker is not None:
            return _blocked_comparison(
                estimate, actual, consensus.provider, provider_result, mapping_blocker
            )
        assert normalization_mapping is not None
        normalized_actual = normalization_mapping.target
        mapping_id = normalization_mapping.mapping_id

    assert normalized_actual.value is not None
    return SurpriseComparison(
        estimate=estimate,
        actual=actual,
        provider=consensus.provider,
        provider_reported=provider_result,
        normalized={
            "value": normalized_actual.value - estimate.value,
            "status": "computed",
        },
        normalization_mapping_id=mapping_id,
    )


def _compatibility_blocker(estimate: MetricValue, actual: MetricValue) -> str | None:
    if not _same_period(estimate, actual):
        return "fiscal_period_mismatch"
    if estimate.unit is not actual.unit:
        return "unit_mismatch"
    if estimate.scope is not actual.scope:
        return "scope_mismatch"
    return None


def _mapping_blocker(
    mapping: NormalizationMapping | None,
    actual: MetricValue,
    estimate: MetricValue,
) -> str | None:
    if mapping is None:
        return "accounting_basis_mismatch"
    if mapping.source != actual:
        return "normalization_mapping_source_mismatch"
    if mapping.target.accounting_basis is not estimate.accounting_basis:
        return "normalization_mapping_target_basis_mismatch"
    if _compatibility_blocker(estimate, mapping.target) is not None:
        return "normalization_mapping_target_incompatible"
    return None


def _blocked_comparison(
    estimate: MetricValue,
    actual: MetricValue,
    provider: str,
    provider_result: _ProviderSurprise | None,
    reason: str,
) -> SurpriseComparison:
    return SurpriseComparison(
        estimate=estimate,
        actual=actual,
        provider=provider,
        provider_reported=provider_result,
        normalized={"value": None, "status": "blocked", "reason": reason},
        blockers=(reason,),
    )


__all__ = [
    "EpsActuals",
    "ConsensusEps",
    "AccountingAdjustment",
    "EarningsQuality",
    "SurpriseComparison",
    "NormalizationMapping",
    "compute_normalized_surprise",
]
