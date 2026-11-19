from pathlib import Path

import pytest

from eventlens.artifacts import (
    FixtureReplayError,
    build_fixture_replay,
    write_fixture_replay,
)


FIXTURES = Path(__file__).parents[2] / "fixtures"


def test_fixture_replay_is_deterministic_and_evidence_linked(tmp_path: Path) -> None:
    first, first_sha = write_fixture_replay(
        ticker="DEMO", event_id="demo-demo-2026q2", fixture_root=FIXTURES,
        output=tmp_path / "first.json",
    )
    second = build_fixture_replay(
        ticker="DEMO", event_id="demo-demo-2026q2", fixture_root=FIXTURES,
    )
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first_sha
    assert first.verify_payload_checksum()
    assert first.payload["report"]["event_count"] == 1
    assert first.source_lineage[0].source_type == "fixture"


def test_unknown_fixture_event_fails_closed() -> None:
    with pytest.raises(FixtureReplayError, match="exactly one event"):
        build_fixture_replay(
            ticker="DEMO", event_id="does-not-exist", fixture_root=FIXTURES,
        )


def test_written_replay_can_be_rehydrated(tmp_path: Path) -> None:
    _, _ = write_fixture_replay(
        ticker="DEMO", event_id="demo-demo-2026q1", fixture_root=FIXTURES,
        output=tmp_path / "replay.json",
    )
    from eventlens.artifacts import AtomicArtifactStore

    restored = AtomicArtifactStore().read(tmp_path / "replay.json")
    assert restored.event_id == "demo-demo-2026q1"
    assert restored.payload["report"]["ticker"] == "DEMO"
