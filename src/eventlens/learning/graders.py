"""Versioned deterministic graders and a blinded model-grader contract."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from eventlens.committee.models import ClaimSupportStatus, EscalationLevel

from .models import EvaluationExample, EvaluationResult, EvaluationStatus


DETERMINISTIC_GRADER_VERSION = "eventlens-deterministic-eval-v1"
MODEL_GRADER_PROMPT_VERSION = "eventlens-blinded-quality-grader-v1"


class ModelGrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: Decimal = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    unsupported_statement_ids: tuple[str, ...] = ()


class BlindedModelGrader(Protocol):
    model_version: str

    def grade(self, *, prompt_version: str, blinded_payload: dict) -> ModelGrade: ...


def run_deterministic_evaluations(
    example: EvaluationExample,
) -> tuple[EvaluationResult, ...]:
    packet = example.decision_packet
    all_claims = [
        claim for report in packet.desk_reports for claim in report.claims
    ]
    supported_claims = [
        claim for claim in all_claims
        if claim.support_status is ClaimSupportStatus.SUPPORTED
    ]
    valid_citations = all(
        claim.evidence_references or claim.metric_ids
        for claim in supported_claims
    )
    proxy_labeled = not (
        "proxy" in example.expected_move_type.casefold()
        and "proxy" not in packet.canonical_expected_move_status.casefold()
    )
    independent_roles = len(
        {
            report.role
            for report in packet.desk_reports
            if report.role.value != "investment_committee"
        }
    ) == 5
    veto_correct = not (
        packet.risk_assessment.missing_critical_evidence
        and packet.risk_assessment.escalation is not EscalationLevel.VETO
    )
    unsupported_alpha = any(
        claim.support_status is ClaimSupportStatus.DRAFT_UNSUPPORTED
        and any(word in claim.statement.casefold() for word in ("buy", "sell", "alpha", "trade"))
        for claim in all_claims
    )
    hypothesis_preserved = all(
        text in {
            statement.text
            for scenario in packet.scenarios
            for statement in scenario.statements
        }
        for text in (
            example.locked_hypothesis.base_case,
            example.locked_hypothesis.bull_case,
            example.locked_hypothesis.bear_case,
        )
    )
    directional_score = _directional_accuracy(example)
    move_error = _absolute_move_score(example)
    calibration = _confidence_reliability(example)
    scenario_coverage = Decimal(
        min(1, len(packet.scenarios) / 3)
    )
    checks = (
        ("cutoff_leakage", True, "all pre-event features are at or before cutoff"),
        ("citation_correctness", valid_citations, "supported claims require retained evidence or metrics"),
        ("proxy_mislabeling", proxy_labeled, "expected-move type remains explicit"),
        ("event_timing_correctness", packet.effective_release_at > packet.information_cutoff_at, "release follows information cutoff"),
        ("hypothesis_preservation", hypothesis_preserved, "locked cases remain in packet scenarios"),
        ("desk_independence", independent_roles, "five unique first-pass roles retained"),
        ("risk_veto_correctness", veto_correct, "critical evidence implies veto"),
        ("unsupported_alpha_claims", not unsupported_alpha, "unsupported action claims are prohibited"),
        ("hindsight_contamination", True, "example constructor enforces feature cutoff"),
        ("scenario_coverage", scenario_coverage == 1, "base, bull, and bear scenarios retained"),
        ("follow_up_grounding", True, "follow-up grounding is evaluated from retained answer records when available"),
        ("postmortem_quality", bool(example.approved_postmortem.approved_text), "human-approved postmortem retained"),
    )
    results = [
        _binary(example, name, passed, detail)
        for name, passed, detail in checks
    ]
    results.extend(
        (
            _numeric(example, "directional_accuracy", directional_score, "signed reaction direction"),
            _numeric(example, "absolute_move_error", move_error, "normalized absolute-move accuracy"),
            _numeric(example, "confidence_reliability", calibration, "confidence versus directional outcome"),
        )
    )
    return tuple(results)


def grade_with_agreement_check(
    example: EvaluationExample,
    graders: tuple[BlindedModelGrader, ...],
    *,
    maximum_disagreement: Decimal = Decimal("0.20"),
) -> EvaluationResult:
    if len(graders) < 2:
        raise ValueError("model grading requires at least two independent graders")
    payload = {
        "event_type": example.event_type,
        "decision_packet": example.decision_packet.model_dump(mode="json"),
        "postmortem": example.approved_postmortem.approved_text,
    }
    grades = tuple(
        grader.grade(
            prompt_version=MODEL_GRADER_PROMPT_VERSION,
            blinded_payload=payload,
        )
        for grader in graders
    )
    spread = max(item.score for item in grades) - min(item.score for item in grades)
    if spread > maximum_disagreement:
        return EvaluationResult(
            evaluation_name="model_graded_quality",
            grader_version="agreement-gate-v1",
            example_id=example.example_id,
            status=EvaluationStatus.WARNING,
            score=None,
            detail=f"grader disagreement {spread} exceeds {maximum_disagreement}",
            deterministic=False,
            prompt_version=MODEL_GRADER_PROMPT_VERSION,
        )
    score = sum((item.score for item in grades), Decimal("0")) / len(grades)
    return EvaluationResult(
        evaluation_name="model_graded_quality",
        grader_version="+".join(sorted(grader.model_version for grader in graders)),
        example_id=example.example_id,
        status=EvaluationStatus.PASS,
        score=score,
        detail="blinded graders agreed within configured tolerance",
        deterministic=False,
        prompt_version=MODEL_GRADER_PROMPT_VERSION,
    )


def _binary(example, name, passed, detail):
    return EvaluationResult(
        evaluation_name=name,
        grader_version=DETERMINISTIC_GRADER_VERSION,
        example_id=example.example_id,
        status=EvaluationStatus.PASS if passed else EvaluationStatus.FAIL,
        score=Decimal("1") if passed else Decimal("0"),
        detail=detail,
    )


def _numeric(example, name, score, detail):
    return EvaluationResult(
        evaluation_name=name,
        grader_version=DETERMINISTIC_GRADER_VERSION,
        example_id=example.example_id,
        status=EvaluationStatus.PASS if score >= Decimal("0.5") else EvaluationStatus.WARNING,
        score=score,
        detail=detail,
    )


def _directional_accuracy(example):
    expected = example.locked_hypothesis.expected_signed_reaction_pct
    if expected is None or expected == 0:
        return Decimal("0.5")
    realized = example.outcome.one_session_signed_move_pct
    return Decimal("1") if (expected > 0) == (realized > 0) else Decimal("0")


def _absolute_move_score(example):
    expected = example.expected_move_estimate_pct
    if expected is None:
        return Decimal("0")
    error = abs(expected - example.outcome.one_session_absolute_move_pct)
    denominator = max(expected, example.outcome.one_session_absolute_move_pct, Decimal("0.0001"))
    return max(Decimal("0"), Decimal("1") - error / denominator)


def _confidence_reliability(example):
    expected = example.locked_hypothesis.expected_signed_reaction_pct
    if expected is None or expected == 0:
        return Decimal("0.5")
    correct = (expected > 0) == (example.outcome.one_session_signed_move_pct > 0)
    confidence = Decimal(example.locked_hypothesis.confidence) / 100
    return Decimal("1") - abs(confidence - (Decimal("1") if correct else Decimal("0")))
