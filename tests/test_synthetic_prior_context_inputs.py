from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "docs" / "acceptance"
CASES = (
    (
        "synthetic-after-market-prior-context.json",
        "synthetic-after-market-rehearsal.json",
    ),
    (
        "synthetic-before-market-prior-context.json",
        "synthetic-before-market-event.json",
    ),
)


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("input_name", "manifest_name"), CASES)
def test_synthetic_prior_context_inputs_match_manifest_and_checksum(
    input_name: str, manifest_name: str
) -> None:
    inputs_path = ACCEPTANCE / input_name
    checksum_path = inputs_path.with_suffix(inputs_path.suffix + ".sha256")
    inputs = _load(inputs_path)
    manifest = _load(ACCEPTANCE / manifest_name)

    assert hashlib.sha256(inputs_path.read_bytes()).hexdigest() == checksum_path.read_text().split()[0]
    assert inputs["dataset_kind"] == "synthetic"
    assert inputs["schema_version"] == "eventlens.historical-context-inputs.v1"
    assert inputs["acceptance_event_manifest"] == f"docs/acceptance/{manifest_name}"
    assert inputs["ticker"] == manifest["issuer"]["ticker"] == "DEMO"  # type: ignore[index]
    assert inputs["company"] == "Example Systems, Inc."
    assert inputs["benchmark_ticker"] == manifest["market"]["benchmark_ticker"] == "SYNIDX"  # type: ignore[index]
    assert inputs["generation_window"]["target_cutoff_at"] == manifest["information_barrier"]["cutoff_at"]  # type: ignore[index]

    source_events = {
        (item["event_date"], item["fiscal_period"]): item["issuer_results_url"]
        for item in manifest["prior_events"]  # type: ignore[index]
    }
    cards = inputs["events"]  # type: ignore[assignment]
    assert len(cards) == 3
    assert len({item["card_id"] for item in cards}) == 3
    assert len({item["event_id"] for item in cards}) == 3
    for item in cards:
        event_date = item["event_id"].split(":")[1]
        period = item["event_id"].split(":")[2]
        assert item["event_id"].startswith("DEMO:")
        assert item["issuer_source_url"] == source_events[(event_date, period)]
        assert item["issuer_source_url"].startswith("https://example.com/")
        assert datetime.fromisoformat(item["historical_event_cutoff_at"]) < datetime.fromisoformat(
            inputs["generation_window"]["target_cutoff_at"]  # type: ignore[index]
        )
        assert item["source_facts"]["revenue_usd_million"]
        assert "synthetic" in item["narrative"]["executive_summary"].casefold()

    assert inputs["review_policy"] == {
        "approval_actor_required": True,
        "cards_emitted_as": "candidate",
        "owner_approval_required_before_attachment": True,
    }
