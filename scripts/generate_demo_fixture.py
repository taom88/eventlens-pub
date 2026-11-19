"""Regenerate the canonical D0 DEMO cards, bundle, report, and manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from eventlens.agents import DeterministicExternalAgentFixture
from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.product.fixture_replay import (
    DEFAULT_FIXTURE_ROOT,
    FixtureBaseline,
    FixtureContextCard,
    FixtureManifest,
    _assemble_fixture_bundle,
)


CARD_FEATURES = {
    "demo-d0-q2-2025": {
        "article_count": 0,
        "source_count": 0,
        "mean_ticker_relevance": None,
        "weighted_sentiment": None,
        "recency_weighted_sentiment": None,
        "sentiment_positive_share": None,
        "latest_close": "231.52",
        "prior_5d_return_pct": "0.014",
        "prior_20d_return_pct": "0.032",
        "missing_values": [
            {"field": field, "reason": "Provider-native news sentiment was not persisted in the key-free fixture replay."}
            for field in (
                "mean_ticker_relevance", "weighted_sentiment",
                "recency_weighted_sentiment", "sentiment_positive_share",
            )
        ],
    },
    "demo-d0-q3-2025": {
        "article_count": 0,
        "source_count": 0,
        "mean_ticker_relevance": None,
        "weighted_sentiment": None,
        "recency_weighted_sentiment": None,
        "sentiment_positive_share": None,
        "latest_close": "213.75",
        "prior_5d_return_pct": "-0.008",
        "prior_20d_return_pct": "0.021",
        "missing_values": [
            {"field": field, "reason": "Provider-native news sentiment was not persisted in the key-free fixture replay."}
            for field in (
                "mean_ticker_relevance", "weighted_sentiment",
                "recency_weighted_sentiment", "sentiment_positive_share",
            )
        ],
    },
    "demo-d0-q4-2025": {
        "article_count": 0,
        "source_count": 0,
        "mean_ticker_relevance": None,
        "weighted_sentiment": None,
        "recency_weighted_sentiment": None,
        "sentiment_positive_share": None,
        "latest_close": "242.05",
        "prior_5d_return_pct": "0.006",
        "prior_20d_return_pct": "-0.017",
        "missing_values": [
            {"field": field, "reason": "Provider-native news sentiment was not persisted in the key-free fixture replay."}
            for field in (
                "mean_ticker_relevance", "weighted_sentiment",
                "recency_weighted_sentiment", "sentiment_positive_share",
            )
        ],
    },
}

CARD_REACTIONS = {
    "demo-d0-q2-2025": {
        "after_hours_return_pct": None,
        "next_session_open_vs_reference_close_pct": "-0.80",
        "t_plus_1_close_return_pct": "-1.10",
        "t_plus_5_close_return_pct": "-0.65",
        "absolute_next_session_open_move_pct": "0.80",
        "missing_values": [{"field": "after_hours_return_pct", "reason": "After-hours price was not retained in the fixture replay."}],
    },
    "demo-d0-q3-2025": {
        "after_hours_return_pct": None,
        "next_session_open_vs_reference_close_pct": "-1.10",
        "t_plus_1_close_return_pct": "-0.90",
        "t_plus_5_close_return_pct": "0.40",
        "absolute_next_session_open_move_pct": "1.10",
        "missing_values": [{"field": "after_hours_return_pct", "reason": "After-hours price was not retained in the fixture replay."}],
    },
    "demo-d0-q4-2025": {
        "after_hours_return_pct": None,
        "next_session_open_vs_reference_close_pct": "-1.70",
        "t_plus_1_close_return_pct": "-1.35",
        "t_plus_5_close_return_pct": "-0.20",
        "absolute_next_session_open_move_pct": "1.70",
        "missing_values": [{"field": "after_hours_return_pct", "reason": "After-hours price was not retained in the fixture replay."}],
    },
}


def _write_json(path: Path, payload: object, *, canonical: bool = False) -> bytes:
    data = (
        canonical_json_bytes(payload) + b"\n"
        if canonical
        else (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    )
    path.write_bytes(data)
    return data


def main() -> None:
    root = DEFAULT_FIXTURE_ROOT
    cards: list[FixtureContextCard] = []
    for filename in (
        "cards/demo-d0-q2-2025.json",
        "cards/demo-d0-q3-2025.json",
        "cards/demo-d0-q4-2025.json",
    ):
        path = root / filename
        raw = json.loads(path.read_text(encoding="utf-8"))
        card_key = raw["card_id"]
        raw["pre_event_features"] = CARD_FEATURES[card_key]
        raw["reaction_metrics"] = CARD_REACTIONS[card_key]
        content = {key: value for key, value in raw.items() if key != "content_sha256"}
        card = FixtureContextCard(**{**content, "content_sha256": sha256_hex(content)})
        _write_json(path, card.model_dump(mode="json"))
        cards.append(card)

    baseline_path = root / "baseline.json"
    baseline = FixtureBaseline.model_validate_json(baseline_path.read_bytes())
    manifest = FixtureManifest.model_validate_json((root / "manifest.json").read_bytes())
    bundle = _assemble_fixture_bundle(manifest, tuple(cards), baseline)
    bundle_bytes = _write_json(root / manifest.bundle_file, bundle.model_dump(mode="json"), canonical=True)
    report = DeterministicExternalAgentFixture().interpret(bundle)
    report_bytes = _write_json(root / manifest.report_file, report.model_dump(mode="json"), canonical=True)
    checksums = {
        filename: hashlib.sha256((root / filename).read_bytes()).hexdigest()
        for filename in (*manifest.card_files, manifest.baseline_file)
    }
    checksums[manifest.bundle_file] = hashlib.sha256(bundle_bytes).hexdigest()
    checksums[manifest.report_file] = hashlib.sha256(report_bytes).hexdigest()
    updated_manifest = manifest.model_copy(update={"checksums": checksums})
    _write_json(root / "manifest.json", updated_manifest.model_dump(mode="json"))


if __name__ == "__main__":
    main()
