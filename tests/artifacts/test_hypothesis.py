from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from eventlens.artifacts import HypothesisAnnotation, HypothesisSnapshot, sha256_hex


STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_lock_returns_new_snapshot_and_preserves_draft() -> None:
    draft = HypothesisSnapshot.draft(
        snapshot_id="hyp-1", session_id="session-1", event_id="event-1", version=1,
        content={"base_case": "durable demand"}, evidence_cutoff_at=STAMP,
    )
    locked = draft.lock(locked_at=STAMP, locked_by="analyst")
    assert draft.status == "draft"
    assert locked.status == "locked"
    assert draft.version == 1
    assert locked.version == 2
    assert locked.content_sha256 == draft.content_sha256
    assert locked.locked_by == "analyst"


def test_locked_snapshot_requires_actor_and_matching_content_checksum() -> None:
    with pytest.raises(ValidationError, match="locked hypothesis requires"):
        HypothesisSnapshot(
            snapshot_id="hyp-1", session_id="session-1", event_id="event-1", version=1,
            status="locked", content={"x": 1}, content_sha256=sha256_hex({"x": 1}),
            evidence_cutoff_at=STAMP,
        )
    draft = HypothesisSnapshot.draft(
        snapshot_id="hyp-1", session_id="session-1", event_id="event-1", version=1,
        content={"x": 1}, evidence_cutoff_at=STAMP,
    )
    with pytest.raises(ValidationError):
        HypothesisSnapshot.model_validate(draft.model_dump() | {"content": {"x": 2}})


def test_annotations_reference_but_do_not_mutate_snapshot() -> None:
    snapshot = HypothesisSnapshot.draft(
        snapshot_id="hyp-1", session_id="session-1", event_id="event-1", version=1,
        content={"x": 1}, evidence_cutoff_at=STAMP,
    )
    note = HypothesisAnnotation.create(
        annotation_id="note-1", snapshot=snapshot, version=1,
        annotation_type="review_note", body="Needs CFO evidence.",
        created_at=STAMP, created_by="analyst",
    )
    assert note.snapshot_id == snapshot.snapshot_id
    assert note.body_sha256
    assert snapshot.content == {"x": 1}


def test_annotation_tampering_is_rejected() -> None:
    snapshot = HypothesisSnapshot.draft(
        snapshot_id="hyp-1", session_id="session-1", event_id="event-1", version=1,
        content={"x": 1}, evidence_cutoff_at=STAMP,
    )
    note = HypothesisAnnotation.create(
        annotation_id="note-1", snapshot=snapshot, version=1,
        annotation_type="review_note", body="Original.", created_at=STAMP, created_by="analyst",
    )
    with pytest.raises(ValidationError, match="annotation body checksum"):
        HypothesisAnnotation.model_validate(note.model_dump() | {"body": "Tampered"})
