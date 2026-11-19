"""MVP-203 context/baseline read-side projection tests."""

from decimal import Decimal

import pytest

from eventlens.web.context_baseline import (
    ContextProjectionError,
    load_context_cards,
    load_deterministic_baseline,
)


def _card(day: str, card_id: str, *, summary: str = "A readable summary") -> dict:
    return {
        "card_id": card_id,
        "ticker": "DEMO",
        "event_id": f"DEMO:{day}",
        "event_date": day,
        "executive_summary": summary,
        "key_messages": ["Growth was described before the event."],
        "feature_snapshot": {"prior_20d_return_pct": "0.10", "prior_5d_return_pct": None},
        "historical_outcome": {"next_open_return_pct": "-0.04"},
        "rights_status": "metadata_only_news",
        "source_provenance": [{"provider_name": "fixture", "raw_response_sha256": "a" * 64}],
    }


def _baseline(**updates: object) -> dict:
    payload = {
        "ticker": "DEMO",
        "event_id": "DEMO:2026-01-01",
        "sample_size": 5,
        "signed_reaction_pct": "-0.10",
        "absolute_reaction_pct": "0.10",
        "confidence_interval": {
            "estimate_pct": "-0.10",
            "lower_pct": "-0.20",
            "upper_pct": "0.01",
            "confidence_level": "0.95",
        },
        "mechanical_guardrails": {"summary": "Entry and trim thresholds only.", "thresholds": ["buy below 100"]},
        "valuation": {"summary": "No valuation model was run.", "methodology": "not_run"},
        "benchmark_supplement": {"summary": "QQQ-relative context.", "benchmark_id": "QQQ", "sample_size": 5},
        "transaction_supplement": {"summary": "Costs are analysis-only.", "sample_size": 5},
    }
    payload.update(updates)
    return payload


@pytest.mark.parametrize("raw", [None, [], {"cards": []}])
def test_context_cards_support_empty_view(raw: object) -> None:
    view = load_context_cards(raw)
    assert view.status == "empty"
    assert view.cards == ()
    assert view.missing_reason


def test_context_cards_support_one_and_three_with_stable_order() -> None:
    one = load_context_cards([_card("2026-02-01", "one")])
    assert len(one.cards) == 1
    assert one.cards[0].executive_summary == "A readable summary"
    three = load_context_cards([_card("2026-03-01", "c"), _card("2026-01-01", "a"), _card("2026-02-01", "b")])
    assert [item.card_id for item in three.cards] == ["a", "b", "c"]


def test_context_card_has_human_content_rights_provenance_and_downloads() -> None:
    card = load_context_cards([_card("2026-01-01", "card")]).cards[0]
    assert card.metric_interpretation.startswith("Plain-language")
    assert card.reaction_interpretation.startswith("Plain-language")
    assert card.rights_warnings == ("metadata_only_news",)
    assert card.provenance[0].checksum == "a" * 64
    assert b"Executive summary" in card.markdown_download.content
    assert card.json_download.content


def test_context_card_malformed_or_checksum_only_input_is_rejected() -> None:
    with pytest.raises(ContextProjectionError):
        load_context_cards([{"card_id": "only-id"}])
    with pytest.raises(ContextProjectionError):
        load_context_cards([_card("2026-01-01", "x"), _card("2026-01-01", "x")])
    with pytest.raises(ContextProjectionError):
        load_context_cards([_card("2026-01-01", "x")] * 4)


def test_baseline_preserves_signed_absolute_labels_and_does_not_derive() -> None:
    view = load_deterministic_baseline(_baseline())
    assert view.signed_reaction.label == "signed"
    assert view.absolute_reaction.label == "absolute"
    assert view.signed_reaction.value_pct == Decimal("-0.10")
    assert view.absolute_reaction.value_pct == Decimal("0.10")
    missing = load_deterministic_baseline(_baseline(absolute_reaction_pct=None))
    assert missing.absolute_reaction.value_pct is None
    assert "not derived" in missing.absolute_reaction.missing_reason


def test_baseline_missing_ci_has_explicit_reason_and_supplements_are_distinct() -> None:
    view = load_deterministic_baseline(_baseline(confidence_interval=None))
    assert view.confidence_interval.missing_reason
    assert view.benchmark_supplement.label == "benchmark"
    assert view.transaction_supplement.label == "transaction_cost"


def test_baseline_keeps_mechanical_guardrails_separate_from_valuation() -> None:
    view = load_deterministic_baseline(_baseline())
    assert view.mechanical_guardrails.summary != view.valuation.summary
    no_valuation = load_deterministic_baseline(_baseline(valuation=None))
    assert not no_valuation.valuation.available
    assert "not valuation" in no_valuation.valuation.missing_reason


def test_baseline_malformed_input_rejected() -> None:
    with pytest.raises(ContextProjectionError):
        load_deterministic_baseline({"sample_size": 3})
    with pytest.raises(ValueError):
        load_deterministic_baseline(_baseline(absolute_reaction_pct="-1"))
