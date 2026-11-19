"""Expected-versus-realized movement comparison with explicit edge handling."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from ._base import ZERO, CalculationWarning, DomainModel
from ._decimal import DECIMAL_CALCULATION_VERSION, canonical_decimal_context
from .volatility import ExpectationMeasureKind


class RealizationStatus(StrEnum):
    EXCEEDED = "realized_exceeded_expected"
    BELOW = "realized_below_expected"
    MATCHED = "realized_matched_expected"
    UNAVAILABLE = "comparison_unavailable"


class ExpectedVsRealizedComparison(DomainModel):
    calculation_version: str = DECIMAL_CALCULATION_VERSION
    calculation_method: str = "realized_absolute_move_minus_expected_move"
    expected_measure_kind: ExpectationMeasureKind
    expected_measure_label: str = Field(min_length=1)
    expected_move_pct: Decimal | None = Field(default=None, ge=ZERO)
    realized_move_pct: Decimal | None = Field(default=None, ge=ZERO)
    move_gap_pct: Decimal | None = None
    realization_ratio: Decimal | None = Field(default=None, ge=ZERO)
    status: RealizationStatus
    warnings: tuple[CalculationWarning, ...] = ()

    @model_validator(mode="after")
    def derived_fields_are_consistent(self) -> "ExpectedVsRealizedComparison":
        if self.expected_measure_label != self.expected_measure_kind.display_label:
            raise ValueError("expected-measure label does not match its typed measure kind")
        if self.expected_move_pct is None or self.realized_move_pct is None:
            if (
                self.move_gap_pct is not None
                or self.realization_ratio is not None
                or self.status is not RealizationStatus.UNAVAILABLE
            ):
                raise ValueError("missing comparison inputs require unavailable derived fields")
            return self
        with canonical_decimal_context():
            expected_gap = self.realized_move_pct - self.expected_move_pct
        if self.move_gap_pct != expected_gap:
            raise ValueError("move_gap_pct does not match realized minus expected")
        if self.expected_move_pct == ZERO:
            if self.realization_ratio is not None or self.status is not RealizationStatus.UNAVAILABLE:
                raise ValueError("zero expected move requires an unavailable ratio")
            return self
        with canonical_decimal_context():
            expected_ratio = self.realized_move_pct / self.expected_move_pct
        expected_status = (
            RealizationStatus.EXCEEDED
            if expected_ratio > Decimal("1")
            else RealizationStatus.BELOW
            if expected_ratio < Decimal("1")
            else RealizationStatus.MATCHED
        )
        if self.realization_ratio != expected_ratio or self.status is not expected_status:
            raise ValueError("realization ratio or status is inconsistent with inputs")
        return self


def compare_expected_and_realized(
    *,
    expected_move_pct: Decimal | None,
    realized_move_pct: Decimal | None,
    expected_measure_kind: ExpectationMeasureKind,
) -> ExpectedVsRealizedComparison:
    """Compare absolute realized movement to a selected expectation measure.

    A zero expected move has a meaningful gap but an undefined ratio.  Missing
    inputs never become zeros: their unavailable result carries a warning.
    """

    expected_measure_label = expected_measure_kind.display_label
    if expected_move_pct is not None and expected_move_pct < ZERO:
        raise ValueError("expected_move_pct must be non-negative")
    if realized_move_pct is not None and realized_move_pct < ZERO:
        raise ValueError("realized_move_pct must be non-negative")

    warnings: list[CalculationWarning] = []
    if expected_move_pct is None:
        warnings.append(
            CalculationWarning(
                code="expected_move_missing",
                message="Expected move is unavailable; comparison was not calculated.",
            )
        )
    if realized_move_pct is None:
        warnings.append(
            CalculationWarning(
                code="realized_move_missing",
                message="Realized move is unavailable; comparison was not calculated.",
            )
        )
    if warnings:
        return ExpectedVsRealizedComparison(
            expected_measure_kind=expected_measure_kind,
            expected_measure_label=expected_measure_label,
            expected_move_pct=expected_move_pct,
            realized_move_pct=realized_move_pct,
            status=RealizationStatus.UNAVAILABLE,
            warnings=tuple(warnings),
        )

    assert expected_move_pct is not None
    assert realized_move_pct is not None
    with canonical_decimal_context():
        gap = realized_move_pct - expected_move_pct
    if expected_move_pct == ZERO:
        return ExpectedVsRealizedComparison(
            expected_measure_kind=expected_measure_kind,
            expected_measure_label=expected_measure_label,
            expected_move_pct=expected_move_pct,
            realized_move_pct=realized_move_pct,
            move_gap_pct=gap,
            status=RealizationStatus.UNAVAILABLE,
            warnings=(
                CalculationWarning(
                    code="zero_expected_move_ratio_undefined",
                    message="Realization ratio is undefined because expected move is zero.",
                ),
            ),
        )

    with canonical_decimal_context():
        ratio = realized_move_pct / expected_move_pct
    status = (
        RealizationStatus.EXCEEDED
        if ratio > Decimal("1")
        else RealizationStatus.BELOW
        if ratio < Decimal("1")
        else RealizationStatus.MATCHED
    )
    return ExpectedVsRealizedComparison(
        expected_measure_kind=expected_measure_kind,
        expected_measure_label=expected_measure_label,
        expected_move_pct=expected_move_pct,
        realized_move_pct=realized_move_pct,
        move_gap_pct=gap,
        realization_ratio=ratio,
        status=status,
    )
