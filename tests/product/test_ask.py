from __future__ import annotations

from pathlib import Path

import pytest

from eventlens.config import EventLensSettings
from eventlens.product.ask import (
    infer_ticker,
    render_research_ask_markdown,
    run_research_ask,
    write_research_markdown,
)

from .test_research_evidence import _bundle


def test_infer_ticker_uses_explicit_symbol_or_instance_aliases() -> None:
    assert infer_ticker("Prepare BETA earnings research") == "BETA"
    assert infer_ticker(
        "Prepare the issuer's earnings research",
        issuer_aliases={"THE ISSUER": "DEMO"},
    ) == "DEMO"
    with pytest.raises(ValueError, match="explicit market symbol"):
        infer_ticker("Prepare the issuer's earnings research")


def test_missing_research_bundle_blocks_every_ticker_identically(tmp_path: Path) -> None:
    result = run_research_ask(
        "Prepare DEMO earnings research",
        settings=EventLensSettings(), output_dir=tmp_path, session_id="missing-evidence",
    )
    assert result.status == "blocked_evidence_missing"
    assert result.stage == "research_evidence"
    assert result.reasoning is not None
    assert result.reasoning["errors"][0]["code"] == "research_evidence_missing"
    assert "no recommendation" in result.next_action


def test_any_issuer_uses_research_bundle_as_only_agent_input(tmp_path: Path) -> None:
    facts = _bundle()
    result = run_research_ask(
        "Prepare DEMO earnings research",
        ticker=facts.target.ticker,
        evidence_bundle=facts,
        settings=EventLensSettings(), output_dir=tmp_path, session_id="universal-test",
    )
    assert result.status == "awaiting_external_agent"
    assert result.stage == "awaiting_external_agent"
    assert result.reasoning is not None
    assert result.reasoning["route"] == "external"
    assert result.reasoning["report"] is None
    assert result.reasoning["research_evidence_bundle_id"] == facts.bundle_id
    assert result.reasoning["research_evidence_sha256"] == facts.bundle_sha256
    assert (tmp_path / "external-agent" / "universal-test" / "handoff-state.json").exists()


def test_research_bundle_ticker_mismatch_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match"):
        run_research_ask(
            "Prepare DEMO earnings research", ticker="OTHER", evidence_bundle=_bundle(),
            settings=EventLensSettings(), output_dir=tmp_path, session_id="wrong-ticker",
        )


def test_historical_context_gate_is_central_and_fail_closed(tmp_path: Path) -> None:
    facts = _bundle()
    forged = facts.model_dump(mode="json")
    for item in forged["missingness"]:
        if item["component"] == "prior_cards":
            item["kind"] = "not_provided"
    forged["newly_covered_issuer"] = None
    from eventlens.artifacts.models import sha256_hex
    from eventlens.product.research_evidence import ResearchEvidenceBundle
    forged["bundle_sha256"] = sha256_hex({
        key: value for key, value in forged.items() if key != "bundle_sha256"
    })
    no_history = ResearchEvidenceBundle.model_validate(forged)
    result = run_research_ask(
        "Prepare DEMO earnings research", ticker="DEMO", evidence_bundle=no_history,
        settings=EventLensSettings(), output_dir=tmp_path, session_id="history-gate",
    )
    assert result.status == "blocked_context_missing"
    assert result.stage == "historical_context_gate"
    assert not (tmp_path / "external-agent" / "history-gate").exists()


def test_research_markdown_surfaces_blocked_reason(tmp_path: Path) -> None:
    payload = {
        "ticker": "DEMO", "session_id": "demo-test", "status": "blocked_evidence_missing",
        "stage": "research_evidence", "model_provider": "external",
        "model": "user_supplied", "created_at": "2026-10-24T21:00:00Z",
        "question": "Prepare earnings research.", "locked_replay": None,
        "reasoning": {"report": None, "errors": [{"code": "research_evidence_missing", "message": "Bundle missing."}]},
        "cards": [], "next_action": "Build evidence.",
    }
    rendered = render_research_ask_markdown(payload)
    assert "No validated model report" in rendered
    assert "research_evidence_missing" in rendered
    output, checksum, digest = write_research_markdown(payload, tmp_path / "run.md")
    assert output.read_text(encoding="utf-8") == rendered
    assert checksum.read_text(encoding="utf-8").strip() == digest
