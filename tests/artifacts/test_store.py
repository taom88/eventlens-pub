from pathlib import Path

import pytest

from datetime import datetime, timezone

from eventlens.artifacts import AtomicArtifactStore, ArtifactEnvelope, ArtifactSourceRef


def _envelope() -> ArtifactEnvelope:
    return ArtifactEnvelope.from_payload(
        artifact_type="event_study",
        artifact_id="artifact-1",
        session_id="session-1",
        event_id="DEMO:2026-01-01:2026Q1",
        source_lineage=(ArtifactSourceRef(source_type="fixture", source_id="demo-fixture-v1", source_sha256="a" * 64),),
        input_sha256="b" * 64,
        calculation_version="m2.4",
        payload={"mean": "0.10"},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_atomic_store_round_trip_and_checksum(tmp_path: Path) -> None:
    target = tmp_path / "artifacts" / "event-study.json"
    store = AtomicArtifactStore()
    digest = store.write(_envelope(), target)
    assert target.exists()
    assert len(digest) == 64
    assert store.read(target) == _envelope()


def test_failed_replace_does_not_create_partial_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "event-study.json"
    store = AtomicArtifactStore()

    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr("eventlens.artifacts.store.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.write(_envelope(), target)
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_tampered_payload_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "event-study.json"
    store = AtomicArtifactStore()
    store.write(_envelope(), target)
    target.write_text(target.read_text().replace('"0.10"', '"9.99"'), encoding="utf-8")
    with pytest.raises(ValueError, match="payload checksum"):
        store.read(target)
