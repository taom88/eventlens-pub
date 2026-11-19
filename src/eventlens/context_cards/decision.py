"""Fail-closed scenario and decision semantics for Context Card v4.

This module validates a proposed decision-support output.  It is deliberately
not a trading policy, brokerage interface, or source of personalized advice.
The policy remains proposed until an analyst reviews it.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .evidence import (
    DEFAULT_EVIDENCE_POLICY,
    AnalyticalPermission,
    AnalyticalUse,
    EvidenceClass,
    EvidencePolicy,
    EvidenceReceipt,
    permission_for,
)


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
UnitInterval = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
PositivePrice = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

METHODOLOGY_STATUS = "proposed_pending_analyst_review"
DECISION_POLICY_VERSION = "eventlens.context_card.decision.proposed.1"


class _DecisionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True
    )


class DecisionType(StrEnum):
    PRE_EARNINGS_EVENT_TRADE = "pre_earnings_event_trade"
    LONG_TERM_INVESTMENT_VIEW = "long_term_investment_view"


class DecisionAction(StrEnum):
    """v4 actions plus exact legacy spellings for persistence compatibility."""

    AVOID_NEW_LONG = "avoid_new_long"
    BUY = "buy"
    HOLD = "hold"
    REDUCE = "reduce"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    LEGACY_BUY = "BUY"
    DONT_BUY = "DONT_BUY"
    TRIM = "TRIM"
    SELL = "SELL"


class ScenarioCalibrationStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    INSUFFICIENT_CALIBRATION = "insufficient_calibration"
    CALIBRATED = "calibrated"


class ThresholdStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_COMPUTED = "not_computed"
    COMPUTED = "computed"
    REMOVED_UNVERIFIABLE = "removed_due_to_unverifiable_derivation"


class ThresholdMethodology(StrEnum):
    SCENARIO_EXPECTED_VALUE = "scenario_expected_value"
    VALUATION = "valuation"
    IMPLIED_MOVE = "implied_move"
    TECHNICAL = "technical"
    USER_CONSTRAINT = "user_constraint"


class DecisionStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    PROPOSED_PENDING_ANALYST_REVIEW = METHODOLOGY_STATUS
    BLOCKED = "blocked"


class ConfidenceLevel(StrEnum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionHorizon(_DecisionModel):
    start: AwareDatetime
    end: AwareDatetime
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def end_follows_start(self) -> "DecisionHorizon":
        if self.end <= self.start:
            raise ValueError("decision horizon end must follow horizon start")
        return self


class DecisionNumericValue(_DecisionModel):
    """Null-safe scalar retained for compatibility with the neutral v4 card."""

    value: FiniteDecimal | None = None
    status: Literal[
        "known", "computed", "not_computed", "unknown", "insufficient_calibration",
        "removed_due_to_unverifiable_derivation",
    ] = "unknown"

    @model_validator(mode="after")
    def status_matches_value(self) -> "DecisionNumericValue":
        if self.status in {"known", "computed"} and self.value is None:
            raise ValueError(f"{self.status} numeric value cannot be null")
        if self.status not in {"known", "computed"} and self.value is not None:
            raise ValueError(f"{self.status} numeric value must remain null")
        return self


class DecisionCriterion(_DecisionModel):
    statement: NonEmptyStr
    evidence_receipt_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_is_unique(self) -> "DecisionCriterion":
        if len(set(self.evidence_receipt_ids)) != len(self.evidence_receipt_ids):
            raise ValueError("criterion evidence receipt ids must be unique")
        return self


class Scenario(_DecisionModel):
    conditions: tuple[NonEmptyStr, ...] = ()
    probability: DecisionNumericValue = Field(
        default_factory=lambda: DecisionNumericValue(status="insufficient_calibration")
    )
    expected_post_event_return: DecisionNumericValue = Field(
        default_factory=lambda: DecisionNumericValue(status="insufficient_calibration")
    )
    calibration_status: ScenarioCalibrationStatus = (
        ScenarioCalibrationStatus.INSUFFICIENT_CALIBRATION
    )
    calibration_methodology: NonEmptyStr | None = None
    calibration_input_receipt_ids: tuple[NonEmptyStr, ...] = ()
    calibrated_at: AwareDatetime | None = None
    triggers: tuple[DecisionCriterion, ...] = ()
    invalidations: tuple[DecisionCriterion, ...] = ()

    @model_validator(mode="after")
    def calibration_is_honest(self) -> "Scenario":
        probability = self.probability.value
        if probability is not None and not Decimal("0") <= probability <= Decimal("1"):
            raise ValueError("scenario probability must be between zero and one")
        if self.calibration_status in {
            ScenarioCalibrationStatus.NOT_EVALUATED,
            ScenarioCalibrationStatus.INSUFFICIENT_CALIBRATION,
        }:
            if probability is not None:
                raise ValueError(
                    "insufficient scenario calibration requires a null probability"
                )
            if self.expected_post_event_return.value is not None:
                raise ValueError(
                    "uncalibrated scenario requires a null expected return"
                )
            if any(
                value not in (None, ())
                for value in (
                    self.calibration_methodology,
                    self.calibration_input_receipt_ids,
                    self.calibrated_at,
                )
            ):
                raise ValueError("uncalibrated scenario cannot retain calibration fields")
        elif self.calibration_status is ScenarioCalibrationStatus.CALIBRATED:
            if probability is None:
                raise ValueError("calibrated scenario requires a probability")
            if any(
                value in (None, ())
                for value in (
                    self.calibration_methodology,
                    self.calibration_input_receipt_ids,
                    self.calibrated_at,
                )
            ):
                raise ValueError(
                    "calibrated scenario requires methodology, evidence inputs, and timestamp"
                )
        if len(set(self.calibration_input_receipt_ids)) != len(
            self.calibration_input_receipt_ids
        ):
            raise ValueError("scenario calibration input receipt ids must be unique")
        return self


class EntryThreshold(_DecisionModel):
    value: FiniteDecimal | None = None
    status: ThresholdStatus = ThresholdStatus.NOT_COMPUTED
    currency: CurrencyCode | None = None
    methodology: ThresholdMethodology | None = None
    inputs: tuple[NonEmptyStr, ...] = ()
    computed_at: AwareDatetime | None = None
    sensitivity: NonEmptyStr | None = None
    methodology_status: Literal[METHODOLOGY_STATUS] = METHODOLOGY_STATUS

    @model_validator(mode="before")
    @classmethod
    def remove_unverifiable_legacy_threshold(cls, raw: object) -> object:
        """Quarantine the unexplained v3 $446.20 scalar instead of preserving it."""

        if isinstance(raw, Mapping):
            values = dict(raw)
            try:
                is_legacy_value = Decimal(str(values.get("value"))) == Decimal("446.20")
            except Exception:
                is_legacy_value = False
            complete = all(
                values.get(field) not in (None, (), [], "")
                for field in ("currency", "methodology", "inputs", "computed_at", "sensitivity")
            )
            if is_legacy_value and not complete:
                return {
                    "value": None,
                    "status": ThresholdStatus.REMOVED_UNVERIFIABLE,
                    "methodology_status": METHODOLOGY_STATUS,
                }
        return raw

    @model_validator(mode="after")
    def derivation_is_complete(self) -> "EntryThreshold":
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("entry-threshold inputs must be unique")
        if self.status is ThresholdStatus.UNKNOWN and self.value is not None:
            raise ValueError("unknown numeric value must remain null")
        required = (
            self.value,
            self.currency,
            self.methodology,
            self.inputs,
            self.computed_at,
            self.sensitivity,
        )
        if self.status is ThresholdStatus.COMPUTED:
            if any(value in (None, (), "") for value in required):
                raise ValueError(
                    "computed entry threshold requires value, currency, methodology, "
                    "inputs, timestamp, and sensitivity"
                )
            if self.value is not None and self.value <= 0:
                raise ValueError("computed entry threshold must be greater than zero")
        elif any(value not in (None, (), "") for value in required):
            raise ValueError("non-computed entry threshold cannot retain derivation fields")
        return self


class LongTermInvestmentView(_DecisionModel):
    action: DecisionAction | None = None
    horizon: DecisionHorizon | None = None
    rationale: NonEmptyStr | None = None
    evidence_receipt_ids: tuple[NonEmptyStr, ...] = ()
    explicitly_separate: Literal[True] = True

    @model_validator(mode="after")
    def view_is_complete_or_neutral(self) -> "LongTermInvestmentView":
        fields = (self.action, self.horizon, self.rationale, self.evidence_receipt_ids)
        if self.action is None and any(value not in (None, ()) for value in fields[1:]):
            raise ValueError("unevaluated long-term view cannot retain decision fields")
        if self.action is not None and any(value in (None, ()) for value in fields[1:]):
            raise ValueError(
                "long-term investment view requires its own horizon, rationale, and evidence"
            )
        return self


class DecisionConfidenceComponents(_DecisionModel):
    factual_integrity: UnitInterval
    temporal_integrity: UnitInterval
    accounting_comparability: UnitInterval
    consensus_quality: UnitInterval
    attribution_quality: UnitInterval
    decision_calibration: UnitInterval


class DecisionInputs(_DecisionModel):
    decision_type: DecisionType = DecisionType.PRE_EARNINGS_EVENT_TRADE
    action: DecisionAction
    horizon: DecisionHorizon
    rationale: NonEmptyStr
    input_evidence_receipt_ids: tuple[NonEmptyStr, ...] = ()
    scenarios: tuple[Scenario, ...] = ()
    entry_threshold: EntryThreshold = Field(default_factory=EntryThreshold)
    reference_price: PositivePrice | None = None
    reference_price_at: AwareDatetime | None = None
    reference_price_evidence_receipt_id: NonEmptyStr | None = None
    currency: CurrencyCode | None = None
    long_term_view: LongTermInvestmentView = Field(default_factory=LongTermInvestmentView)
    confidence: DecisionConfidenceComponents
    decided_at: AwareDatetime
    policy_version: Literal[DECISION_POLICY_VERSION] = DECISION_POLICY_VERSION

    @model_validator(mode="after")
    def inputs_are_reproducible(self) -> "DecisionInputs":
        if len(set(self.input_evidence_receipt_ids)) != len(self.input_evidence_receipt_ids):
            raise ValueError("decision input evidence receipt ids must be unique")
        reference = (
            self.reference_price,
            self.reference_price_at,
            self.reference_price_evidence_receipt_id,
            self.currency,
        )
        if any(value is not None for value in reference) and any(
            value is None for value in reference
        ):
            raise ValueError(
                "reference price requires value, currency, timestamp, and evidence receipt"
            )
        if (
            self.action is not DecisionAction.INSUFFICIENT_EVIDENCE
            and not self.input_evidence_receipt_ids
        ):
            raise ValueError("decision action requires named evidence inputs")
        probabilities = [
            scenario.probability.value
            for scenario in self.scenarios
            if scenario.probability.value is not None
        ]
        if probabilities and len(probabilities) == len(self.scenarios):
            if sum(probabilities) != Decimal("1"):
                raise ValueError("fully calibrated scenario probabilities must sum to one")
        return self


class DecisionTrace(_DecisionModel):
    input_evidence_receipt_ids: tuple[NonEmptyStr, ...]
    threshold_input_receipt_ids: tuple[NonEmptyStr, ...] = ()
    scenario_calibration_evidence_receipt_ids: tuple[NonEmptyStr, ...] = ()
    trigger_evidence_receipt_ids: tuple[NonEmptyStr, ...] = ()
    invalidation_evidence_receipt_ids: tuple[NonEmptyStr, ...] = ()
    long_term_evidence_receipt_ids: tuple[NonEmptyStr, ...] = ()
    traced_at: AwareDatetime
    analytical_use: Literal["autonomous_decision"] = "autonomous_decision"
    policy_version: Literal[DECISION_POLICY_VERSION] = DECISION_POLICY_VERSION

    @property
    def all_evidence_receipt_ids(self) -> tuple[str, ...]:
        return _unique(
            self.input_evidence_receipt_ids
            + self.threshold_input_receipt_ids
            + self.scenario_calibration_evidence_receipt_ids
            + self.trigger_evidence_receipt_ids
            + self.invalidation_evidence_receipt_ids
            + self.long_term_evidence_receipt_ids
        )


class DecisionOutput(_DecisionModel):
    status: DecisionStatus = DecisionStatus.NOT_EVALUATED
    decision_type: DecisionType | None = None
    action: DecisionAction | None = None
    horizon: DecisionHorizon | None = None
    rationale: NonEmptyStr | None = None
    scenarios: tuple[Scenario, ...] = ()
    entry_threshold: EntryThreshold = Field(default_factory=EntryThreshold)
    reference_price: PositivePrice | None = None
    reference_price_at: AwareDatetime | None = None
    currency: CurrencyCode | None = None
    long_term_view: LongTermInvestmentView = Field(default_factory=LongTermInvestmentView)
    confidence: DecisionConfidenceComponents | None = None
    overall_confidence: UnitInterval | None = None
    overall_confidence_level: ConfidenceLevel = ConfidenceLevel.UNKNOWN
    trace: DecisionTrace | None = None
    methodology_status: Literal[METHODOLOGY_STATUS] = METHODOLOGY_STATUS
    decision_support_notice: Literal[
        "Decision support only; not personalized financial advice or trade execution."
    ] = "Decision support only; not personalized financial advice or trade execution."

    @model_validator(mode="before")
    @classmethod
    def neutral_payload_cannot_smuggle_an_action(cls, raw: object) -> object:
        if isinstance(raw, Mapping):
            status = raw.get("status", DecisionStatus.NOT_EVALUATED)
            if raw.get("action") is not None and status == DecisionStatus.NOT_EVALUATED:
                raise ValueError(
                    "Extra inputs are not permitted on a not-evaluated decision"
                )
        return raw

    @model_validator(mode="after")
    def output_is_neutral_or_traceable(self) -> "DecisionOutput":
        if self.status is DecisionStatus.NOT_EVALUATED:
            material = (
                self.decision_type, self.action, self.horizon, self.rationale,
                self.reference_price, self.reference_price_at, self.currency,
                self.confidence, self.overall_confidence, self.trace,
            )
            if any(value is not None for value in material):
                raise ValueError("not-evaluated decision cannot retain decision output")
            if self.scenarios or self.long_term_view.action is not None:
                raise ValueError("not-evaluated decision cannot retain scenario or long-term output")
            if self.entry_threshold.status is ThresholdStatus.COMPUTED:
                raise ValueError("not-evaluated decision cannot retain a computed threshold")
            if self.overall_confidence_level is not ConfidenceLevel.UNKNOWN:
                raise ValueError("not-evaluated decision confidence must remain unknown")
        else:
            if any(
                value is None
                for value in (
                    self.decision_type, self.action, self.horizon, self.rationale,
                    self.confidence, self.overall_confidence, self.trace,
                )
            ):
                raise ValueError("decision-bearing output requires horizon, confidence, and trace")
            assert self.confidence is not None
            expected_overall = _aggregate_confidence(self.confidence)
            if self.overall_confidence != expected_overall:
                raise ValueError("overall decision confidence must use the deterministic policy")
            expected_level = (
                ConfidenceLevel.HIGH
                if expected_overall >= Decimal("0.75")
                else ConfidenceLevel.MEDIUM
                if expected_overall >= Decimal("0.50")
                else ConfidenceLevel.LOW
            )
            if self.overall_confidence_level is not expected_level:
                raise ValueError("overall confidence label does not match its components")
        return self


def trace_decision(
    inputs: DecisionInputs | Mapping[str, object],
    evidence_registry: object,
    *,
    policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
) -> DecisionTrace:
    """Resolve and authorize every fact capable of influencing the proposal."""

    parsed = inputs if isinstance(inputs, DecisionInputs) else DecisionInputs.model_validate(inputs)
    trigger_ids = _unique(
        tuple(
            evidence_id
            for scenario in parsed.scenarios
            for criterion in scenario.triggers
            for evidence_id in criterion.evidence_receipt_ids
        )
    )
    invalidation_ids = _unique(
        tuple(
            evidence_id
            for scenario in parsed.scenarios
            for criterion in scenario.invalidations
            for evidence_id in criterion.evidence_receipt_ids
        )
    )
    threshold_ids = parsed.entry_threshold.inputs
    calibration_ids = _unique(
        tuple(
            evidence_id
            for scenario in parsed.scenarios
            for evidence_id in scenario.calibration_input_receipt_ids
        )
    )
    long_term_ids = parsed.long_term_view.evidence_receipt_ids
    direct_ids = parsed.input_evidence_receipt_ids
    if parsed.reference_price_evidence_receipt_id is not None:
        direct_ids = _unique(direct_ids + (parsed.reference_price_evidence_receipt_id,))
    all_ids = _unique(
        direct_ids
        + threshold_ids
        + calibration_ids
        + trigger_ids
        + invalidation_ids
        + long_term_ids
    )
    receipts = getattr(evidence_registry, "receipts_by_id", None)
    if not isinstance(receipts, Mapping):
        raise TypeError("evidence_registry must expose receipts_by_id")
    for receipt_id in all_ids:
        receipt = receipts.get(receipt_id)
        if not isinstance(receipt, EvidenceReceipt):
            raise ValueError(f"decision evidence receipt {receipt_id!r} cannot be resolved")
        if receipt.evidence_class in {EvidenceClass.RECONSTRUCTED, EvidenceClass.ATTRIBUTED}:
            raise PermissionError(
                f"{receipt.evidence_class.value} evidence cannot drive a decision"
            )
        permission = permission_for(
            receipt,
            AnalyticalUse.AUTONOMOUS_DECISION,
            policy=policy,
            registry=evidence_registry,
        )
        if permission is not AnalyticalPermission.ALLOWED:
            raise PermissionError(
                f"evidence receipt {receipt_id!r} is not permitted for autonomous_decision"
            )
    return DecisionTrace(
        input_evidence_receipt_ids=direct_ids,
        threshold_input_receipt_ids=threshold_ids,
        scenario_calibration_evidence_receipt_ids=calibration_ids,
        trigger_evidence_receipt_ids=trigger_ids,
        invalidation_evidence_receipt_ids=invalidation_ids,
        long_term_evidence_receipt_ids=long_term_ids,
        traced_at=parsed.decided_at,
    )


def build_decision(
    inputs: DecisionInputs | Mapping[str, object],
    evidence_registry: object,
    *,
    policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
) -> DecisionOutput:
    """Build a reproducible proposal; this does not approve or execute it."""

    parsed = inputs if isinstance(inputs, DecisionInputs) else DecisionInputs.model_validate(inputs)
    trace = trace_decision(parsed, evidence_registry, policy=policy)
    overall = _aggregate_confidence(parsed.confidence)
    level = (
        ConfidenceLevel.HIGH
        if overall >= Decimal("0.75")
        else ConfidenceLevel.MEDIUM
        if overall >= Decimal("0.50")
        else ConfidenceLevel.LOW
    )
    return DecisionOutput(
        status=DecisionStatus.PROPOSED_PENDING_ANALYST_REVIEW,
        decision_type=parsed.decision_type,
        action=parsed.action,
        horizon=parsed.horizon,
        rationale=parsed.rationale,
        scenarios=parsed.scenarios,
        entry_threshold=parsed.entry_threshold,
        reference_price=parsed.reference_price,
        reference_price_at=parsed.reference_price_at,
        currency=parsed.currency,
        long_term_view=parsed.long_term_view,
        confidence=parsed.confidence,
        overall_confidence=overall,
        overall_confidence_level=level,
        trace=trace,
    )


def _aggregate_confidence(components: DecisionConfidenceComponents) -> Decimal:
    weights = {
        "factual_integrity": Decimal("0.25"),
        "temporal_integrity": Decimal("0.25"),
        "accounting_comparability": Decimal("0.15"),
        "consensus_quality": Decimal("0.15"),
        "attribution_quality": Decimal("0.10"),
        "decision_calibration": Decimal("0.10"),
    }
    weighted = sum(getattr(components, name) * weight for name, weight in weights.items())
    critical = ("factual_integrity", "temporal_integrity", "accounting_comparability")
    if any(getattr(components, name) < Decimal("0.50") for name in critical):
        weighted = min(weighted, Decimal("0.59"))
    return weighted.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "DECISION_POLICY_VERSION",
    "METHODOLOGY_STATUS",
    "ConfidenceLevel",
    "DecisionAction",
    "DecisionConfidenceComponents",
    "DecisionCriterion",
    "DecisionHorizon",
    "DecisionInputs",
    "DecisionOutput",
    "DecisionTrace",
    "DecisionStatus",
    "DecisionType",
    "EntryThreshold",
    "LongTermInvestmentView",
    "Scenario",
    "ScenarioCalibrationStatus",
    "ThresholdMethodology",
    "ThresholdStatus",
    "build_decision",
    "trace_decision",
]
