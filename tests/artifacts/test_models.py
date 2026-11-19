from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from eventlens.artifacts import ArtifactEnvelope, ArtifactSourceRef, sha256_hex


def _source() -> ArtifactSourceRef:
    return ArtifactSourceRef(
        source_type="fixture",
        source_id="demo-fixture-v1",
        source_sha256="a" * 64,
    )


def _envelope(payload: dict[str, object] | None = None) -> ArtifactEnvelope:
    return ArtifactEnvelope.from_payload(
        artifact_type="event_study",
        artifact_id="artifact-1",
        session_id="session-1",
        event_id="DEMO:2026-01-01:2026Q1",
        source_lineage=(_source(),),
        input_sha256="b" * 64,
        calculation_version="m2.4",
        payload=payload or {"mean": "0.10"},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_envelope_is_strict_immutable_and_checksum_valid() -> None:
    envelope = _envelope()
    assert envelope.verify_payload_checksum()
    assert envelope.canonical_bytes() == _envelope().canonical_bytes()
    with pytest.raises(ValidationError):
        ArtifactEnvelope.model_validate({**envelope.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        envelope.created_at = datetime.now(timezone.utc)  # type: ignore[misc]


def test_payload_tampering_is_detected() -> None:
    envelope = _envelope()
    tampered = envelope.model_copy(update={"payload": {"mean": "9.99"}})
    assert not tampered.verify_payload_checksum()


def test_required_lineage_versions_and_sha_are_validated() -> None:
    with pytest.raises(ValidationError):
        ArtifactEnvelope(
            artifact_type="event_study",
            artifact_id="artifact-1",
            session_id="session-1",
            event_id="event-1",
            created_at=datetime(2026, 1, 1),
            source_lineage=(),
            input_sha256="not-a-sha",
            calculation_version="m1",
            prompt_version="not_applicable",
            payload_sha256=sha256_hex({}),
            payload={},
        )
