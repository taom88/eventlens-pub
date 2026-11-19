from datetime import datetime, timezone
from pathlib import Path

from eventlens.learning.readiness import audit_learning_readiness


def test_empty_repository_is_evaluation_and_shadow_only(tmp_path: Path) -> None:
    audit = audit_learning_readiness(
        tmp_path,
        minimum_complete_events=50,
        clock=lambda: datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    assert audit.ready_for_training is False
    assert audit.permitted_mode == "evaluation_and_shadow_only"
    assert audit.complete_outcome_count == 0
    assert any("no immutable post-event" in reason for reason in audit.reasons)


def test_duplicate_packets_do_not_count_as_unique_training_events(
    tmp_path: Path,
) -> None:
    for session in ("one", "two"):
        target = tmp_path / session / "event"
        target.mkdir(parents=True)
        (target / "decision-packet.json").write_text(
            '{"schema_version":"1.0","ticker":"DEMO","event_id":"same",'
            '"snapshot":{"x":1},"desk_reports":[1],"risk_assessment":{"x":1},'
            '"disagreement_register":{"x":1},"canonical_expected_move_status":"proxy"}',
            encoding="utf-8",
        )
    audit = audit_learning_readiness(tmp_path, minimum_complete_events=2)
    assert audit.persisted_packet_count == 2
    assert audit.unique_event_count == 1
    assert audit.ready_for_training is False
    assert any("duplicate replay" in reason for reason in audit.reasons)
