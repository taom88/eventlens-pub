from eventlens.agents.models import EvidenceOrigin
from eventlens.agents.tools import build_context_card_evidence


def _card(summary=None, messages=()):
    return {
        "card_id": "card-1",
        "event_date": "2026-06-24",
        "knowledge_cutoff_at": "2026-06-25T00:00:00Z",
        "content_sha256": "a" * 64,
        "executive_summary": summary,
        "key_messages": list(messages),
    }


def test_context_card_evidence_retains_summary_and_messages() -> None:
    evidence = build_context_card_evidence([_card("Summary", ("Message",))])
    assert [item.passage_id for item in evidence] == ["executive-summary", "key-message-0"]
    assert all(item.origin is EvidenceOrigin.PROVIDER for item in evidence)


def test_context_card_evidence_handles_missing_summary() -> None:
    evidence = build_context_card_evidence([_card(None, ("Message",))])
    assert [item.passage_id for item in evidence] == ["key-message-0"]
