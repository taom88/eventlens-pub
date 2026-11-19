from pathlib import Path

import pytest

pytest.importorskip("exchange_calendars")

from eventlens.app.commands import main
from eventlens.artifacts import AtomicArtifactStore


def test_replay_cli_writes_rehydratable_fixture_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "demo-replay.json"
    assert main(
        [
            "--env-file", str(root / "does-not-exist.env"),
            "replay", "--ticker", "DEMO", "--event", "demo-demo-2026q2",
            "--fixture-root", str(root / "fixtures"), "--output", str(output),
        ]
    ) == 0
    document = __import__("json").loads(capsys.readouterr().out)
    assert document["payload_checksum_valid"] is True
    restored = AtomicArtifactStore().read(output)
    assert restored.event_id == "demo-demo-2026q2"


def test_replay_cli_rejects_unknown_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).parents[2]
    assert main(
        [
            "--env-file", str(root / "does-not-exist.env"),
            "replay", "--ticker", "DEMO", "--event", "missing",
            "--fixture-root", str(root / "fixtures"), "--output", str(tmp_path / "x.json"),
        ]
    ) == 1
    assert "FixtureReplayError" in capsys.readouterr().out
