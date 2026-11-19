from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    ConfidenceLevel,
    DecisionAction,
    DecisionConfidenceComponents,
    DecisionHorizon,
    DecisionInputs,
    EntryThreshold,
    EvidenceClass,
    EvidenceReceipt,
    EvidenceRegistry,
    LongTermInvestmentView,
    Scenario,
    ScenarioCalibrationStatus,
    build_decision,
)
from eventlens.context_cards.decision import DecisionCriterion


NOW = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)


def _receipt(receipt_id: str, evidence_class: EvidenceClass = EvidenceClass.SOURCE_NATIVE) -> EvidenceReceipt:
    return EvidenceReceipt(
        id=receipt_id,
        evidence_class=evidence_class,
        claim=f"Evidence for {receipt_id}.",
        source_ref=f"source:{receipt_id}",
        temporal_context_ref=f"temporal:{receipt_id}",
    )


def _confidence(*, factual: str = "0.90") -> DecisionConfidenceComponents:
    return DecisionConfidenceComponents(
        factual_integrity=factual,
        temporal_integrity="0.90",
        accounting_comparability="0.90",
        consensus_quality="0.80",
        attribution_quality="0.70",
        decision_calibration="0.60",
    )


def _inputs(**updates: object) -> DecisionInputs:
    values: dict[str, object] = {
        "action": DecisionAction.AVOID_NEW_LONG,
        "horizon": DecisionHorizon(
            start=NOW,
            end=NOW + timedelta(days=2),
            rationale="The event-trade window ends after the first full session.",
        ),
        "rationale": "Current evidence does not clear the proposed event-risk policy.",
        "input_evidence_receipt_ids": ("current",),
        "confidence": _confidence(),
        "decided_at": NOW,
    }
    values.update(updates)
    return DecisionInputs.model_validate(values)


def test_decision_requires_explicit_ordered_horizon() -> None:
    with pytest.raises(ValidationError, match="horizon"):
        DecisionInputs.model_validate(
            {
                "action": "avoid_new_long",
                "rationale": "No horizon was supplied.",
                "input_evidence_receipt_ids": ["current"],
                "confidence": _confidence().model_dump(),
                "decided_at": NOW,
            }
        )


def test_event_trade_and_long_term_view_have_distinct_horizons() -> None:
    long_term = LongTermInvestmentView(
        action=DecisionAction.HOLD,
        horizon=DecisionHorizon(
            start=NOW,
            end=NOW + timedelta(days=365 * 5),
            rationale="Five-year investment horizon.",
        ),
        rationale="Long-term view is assessed independently of the earnings trade.",
        evidence_receipt_ids=("long-term",),
    )
    registry = EvidenceRegistry(
        receipts=(_receipt("current"), _receipt("long-term")),
    )

    output = build_decision(_inputs(long_term_view=long_term), registry)

    assert output.horizon != output.long_term_view.horizon
    assert output.long_term_view.explicitly_separate is True
    assert output.methodology_status == "proposed_pending_analyst_review"


def test_threshold_requires_full_derivation_and_legacy_scalar_is_removed() -> None:
    with pytest.raises(ValidationError, match="methodology, inputs, timestamp, and sensitivity"):
        EntryThreshold(value="440", status="computed", currency="USD")

    removed = EntryThreshold.model_validate({"value": "446.20", "status": "computed"})
    assert removed.value is None
    assert removed.status.value == "removed_due_to_unverifiable_derivation"

    computed = EntryThreshold(
        value="440",
        status="computed",
        currency="USD",
        methodology="scenario_expected_value",
        inputs=("threshold-input",),
        computed_at=NOW,
        sensitivity="A 10% lower bear return reduces the threshold by $8.",
    )
    assert computed.value == Decimal("440")


def test_probability_remains_null_when_calibration_is_insufficient() -> None:
    scenario = Scenario(calibration_status=ScenarioCalibrationStatus.INSUFFICIENT_CALIBRATION)
    assert scenario.probability.value is None

    with pytest.raises(ValidationError, match="requires a null probability"):
        Scenario(
            calibration_status="insufficient_calibration",
            probability={"value": "0.50", "status": "computed"},
        )

    with pytest.raises(ValidationError, match="methodology, evidence inputs, and timestamp"):
        Scenario(
            calibration_status="calibrated",
            probability={"value": "0.50", "status": "computed"},
        )


def test_trace_resolves_triggers_invalidations_and_threshold_inputs() -> None:
    scenario = Scenario(
        calibration_status="insufficient_calibration",
        triggers=(DecisionCriterion(statement="cloud reaccelerates.", evidence_receipt_ids=("trigger",)),),
        invalidations=(DecisionCriterion(statement="Guidance weakens.", evidence_receipt_ids=("invalidate",)),),
    )
    threshold = EntryThreshold(
        value="440", status="computed", currency="USD", methodology="valuation",
        inputs=("threshold-input",), computed_at=NOW,
        sensitivity="A one-turn multiple contraction lowers the threshold by $12.",
    )
    registry = EvidenceRegistry(
        receipts=tuple(
            _receipt(receipt_id)
            for receipt_id in ("current", "trigger", "invalidate", "threshold-input")
        )
    )

    output = build_decision(
        _inputs(scenarios=(scenario,), entry_threshold=threshold),
        registry,
    )

    assert output.trace is not None
    assert set(output.trace.all_evidence_receipt_ids) == {
        "current", "trigger", "invalidate", "threshold-input"
    }


@pytest.mark.parametrize("evidence_class", [EvidenceClass.RECONSTRUCTED, EvidenceClass.ATTRIBUTED])
def test_reconstructed_or_attributed_evidence_cannot_drive_decision(
    evidence_class: EvidenceClass,
) -> None:
    registry = EvidenceRegistry(receipts=(_receipt("current", evidence_class),))

    with pytest.raises(PermissionError, match="cannot drive a decision"):
        build_decision(_inputs(), registry)


def test_critical_low_confidence_prevents_high_overall_confidence() -> None:
    registry = EvidenceRegistry(receipts=(_receipt("current"),))

    output = build_decision(_inputs(confidence=_confidence(factual="0.10")), registry)

    assert output.overall_confidence == Decimal("0.5900")
    assert output.overall_confidence_level is ConfidenceLevel.MEDIUM


def test_legacy_action_values_remain_parseable_without_becoming_policy_approved() -> None:
    assert DecisionAction("BUY") is DecisionAction.LEGACY_BUY
    output = build_decision(
        _inputs(action=DecisionAction.LEGACY_BUY),
        EvidenceRegistry(receipts=(_receipt("current"),)),
    )
    assert output.action is DecisionAction.LEGACY_BUY
    assert output.status.value == "proposed_pending_analyst_review"
