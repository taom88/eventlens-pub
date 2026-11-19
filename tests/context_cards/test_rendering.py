from __future__ import annotations

import hashlib
import json

from eventlens.context_cards import (
    ContextCardV4,
    render_context_card_markdown,
    write_context_card_artifacts,
)


def _card() -> dict[str, object]:
    generated_at = "2026-09-09T03:42:16+00:00"
    return {
        "schema_version": "eventlens.context_card.v4",
        "identity": {"ticker": "DEMO", "company": "Example Systems Corporation", "event": {"type": "earnings", "fiscal_period": "FY2027_Q1", "event_datetime": None, "timezone": None, "event_datetime_status": "unknown"}, "card": {"version": 4, "generated_at": generated_at, "as_of": "2026-09-09T00:00:00+00:00", "sha256": None, "status": "draft"}},
        "temporal_state": {"historical_event_cutoff": "2026-09-08T20:00:00+00:00", "observations": {}},
        "decision_context": {"decision_type": "pre_earnings_event_trade", "reference_price": {"value": None, "status": "unknown"}, "entry_threshold": {"value": None, "status": "not_computed"}, "currency": "USD", "long_term_view_explicitly_separate": True, "decision_status": "not_evaluated"},
        "current_pre_event_state": {}, "company_ontology": {"ontology_id": "demo_kpi_ontology", "version": "1.0.0-proposed.1", "reference": "package://eventlens.context_cards.ontologies/DEMO.yaml"},
        "historical_quarters": [], "management_guidance_ledger": [], "consensus_state": {}, "market_reaction_history": [], "thesis_state": {}, "scenario_model": {}, "decision": {}, "evidence_registry": {}, "conflicts": [], "quality_control": {},
        "provenance": {"schema_version": "eventlens.context_card.v4", "ontology_version": "1.0.0-proposed.1", "generated_at": generated_at},
    }


def test_renderer_has_prescribed_order_unknowns_and_four_history_rows() -> None:
    markdown = render_context_card_markdown(_card())
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]

    assert headings == [
        f"## {number}. {title}"
        for number, title in enumerate((
            "Card identity", "Approval / QC state", "Executive decision context",
            "Current pre-event state", "Current expectations", "Current management guidance",
            "Primary unresolved questions", "Last four quarter comparison", "Guidance ledger",
            "Market reaction patterns", "Accounting adjustments", "Scenario state", "Decision",
            "Evidence receipts", "Data-quality warnings", "Provenance",
        ), start=1)
    ]
    assert "Reference price: unknown" in markdown
    assert "Entry threshold: not computed" in markdown
    table_rows = [line for line in markdown.splitlines() if line.startswith("| unknown / not computed")]
    assert len(table_rows) == 4


def test_artifacts_are_deterministic_and_revision_addressed(tmp_path) -> None:
    card = ContextCardV4.model_validate(_card())
    first = write_context_card_artifacts(card, tmp_path)
    second = write_context_card_artifacts(card, tmp_path)

    assert first == second
    json_bytes = first.json_path.read_bytes()
    assert first.json_sha256 == hashlib.sha256(json_bytes).hexdigest()
    assert f"`{first.json_sha256}`" in first.markdown_path.read_text()
    assert json.loads(json_bytes) == card.model_dump(mode="json")

    revised = _card()
    revised["identity"]["card"]["generated_at"] = "2026-09-10T03:42:16+00:00"  # type: ignore[index]
    revised["provenance"]["generated_at"] = "2026-09-10T03:42:16+00:00"  # type: ignore[index]
    later = write_context_card_artifacts(revised, tmp_path)
    assert later.json_path != first.json_path
    assert first.json_path.read_bytes() == json_bytes
