from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eventlens.committee.models import (
    EscalationLevel,
    EvidenceSnapshot,
    RiskAssessment,
)
from eventlens.committee.packet import DecisionPacket
from eventlens.domain.events import EventTiming
from eventlens.graph.models import AnalystHypothesis
from eventlens.learning.dataset import migrate_evaluation_document
from eventlens.learning.graders import (
    MODEL_GRADER_PROMPT_VERSION,
    ModelGrade,
    grade_with_agreement_check,
)
from eventlens.learning.models import (
    ApprovedPostmortem,
    EvaluationExample,
    EvaluationStatus,
    OutcomeRecord,
    PreEventFeature,
    evaluation_content_checksum,
)


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def _example(*, feature_at: datetime = NOW) -> EvaluationExample:
    snapshot = EvidenceSnapshot.model_construct(
        snapshot_id="snapshot-1",
        event_id="event-1",
        ticker="DEMO",
        information_cutoff_at=NOW,
        reported_timing=EventTiming.AFTER_MARKET_CLOSE,
        effective_timing=EventTiming.AFTER_MARKET_CLOSE,
        release_timestamp_checksum="a" * 64,
        evidence=(),
        canonical_metrics=(),
        evidence_gaps=(),
    )
    risk = RiskAssessment.model_construct(
        report_id="risk-1",
        escalation=EscalationLevel.NONE,
        reasons=(),
        missing_critical_evidence=False,
        timing_uncertainty=False,
        stale_data=False,
        excessive_model_agreement=False,
        unstable_estimates=False,
        insufficient_comparable_events=False,
        unavailable_desks=False,
        cutoff_failure=False,
        unconfirmed_release_timestamp=False,
    )
    packet = DecisionPacket.model_construct(
        event_id="event-1",
        ticker="DEMO",
        desk_reports=(),
        scenarios=(),
        snapshot=snapshot,
        risk_assessment=risk,
    )
    outcome = OutcomeRecord(
        outcome_id="outcome-1",
        session_id="session-1",
        event_id="event-1",
        ticker="DEMO",
        event_date=date(2026, 7, 25),
        collected_at=NOW + timedelta(days=7),
        one_session_signed_move_pct=Decimal("0.02"),
        one_session_absolute_move_pct=Decimal("0.02"),
        five_session_signed_move_pct=Decimal("0.03"),
        five_session_absolute_move_pct=Decimal("0.03"),
        source_checksums=("a" * 64,),
    )
    postmortem = ApprovedPostmortem(
        postmortem_id="post-1",
        outcome_id=outcome.outcome_id,
        draft_text="Draft.",
        approved_text="Approved.",
        approved_by="human",
        approved_at=NOW + timedelta(days=8),
    )
    hypothesis = AnalystHypothesis(
        base_case="Base",
        bull_case="Bull",
        bear_case="Bear",
        key_debate_points=("debate",),
        confidence=50,
        recorded_at=NOW - timedelta(days=1),
    )
    payload = {
        "schema_version": "1.0",
        "example_id": "example-1",
        "created_at": NOW + timedelta(days=8),
        "information_cutoff_at": NOW,
        "session_id": "session-1",
        "event_id": "event-1",
        "ticker": "DEMO",
        "event_date": date(2026, 7, 25),
        "pre_event_features": (
            PreEventFeature(
                feature_id="feature",
                value="x",
                available_at=feature_at,
                source_id="source",
            ),
        ),
        "locked_hypothesis": hypothesis,
        "decision_packet": packet,
        "expected_move_type": "unavailable",
        "outcome": outcome,
        "approved_postmortem": postmortem,
        "provider_availability": {},
        "model_versions": ("fixture",),
        "prompt_versions": ("v1",),
        "quant_parameters": {},
        "human_approvals": ("postmortem",),
    }
    checksum = evaluation_content_checksum(
        EvaluationExample.model_construct(
            **payload, content_sha256="0" * 64
        ).model_dump(mode="json", exclude={"content_sha256"})
    )
    example = EvaluationExample.model_construct(
        **payload, content_sha256=checksum
    )
    if feature_at > NOW:
        example.enforce_cutoff_and_identity()
    return example


def test_post_event_feature_is_rejected_from_pre_event_dataset() -> None:
    with pytest.raises(ValueError, match="post-event evidence"):
        _example(feature_at=NOW + timedelta(seconds=1))


def test_schema_migration_is_explicit_and_rejects_unknown_versions() -> None:
    assert migrate_evaluation_document({"schema_version": "1.0", "x": 1})["x"] == 1
    with pytest.raises(ValueError, match="unsupported"):
        migrate_evaluation_document({"schema_version": "2.0"})


class _Grader:
    def __init__(self, model_version: str, score: str):
        self.model_version = model_version
        self._score = Decimal(score)

    def grade(self, *, prompt_version: str, blinded_payload: dict) -> ModelGrade:
        assert prompt_version == MODEL_GRADER_PROMPT_VERSION
        assert "session_id" not in blinded_payload
        return ModelGrade(score=self._score, rationale="structured")


def test_model_grader_versioning_and_disagreement_gate() -> None:
    result = grade_with_agreement_check(
        _example(),
        (_Grader("grader-a-v1", "0.9"), _Grader("grader-b-v1", "0.3")),
    )
    assert result.status is EvaluationStatus.WARNING
    assert result.score is None
    assert result.prompt_version == MODEL_GRADER_PROMPT_VERSION
