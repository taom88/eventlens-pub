from pathlib import Path

import pytest

from eventlens.agents import DeterministicExternalAgentFixture, ExternalAgentHandoffStore
from eventlens.config import EventLensSettings
from eventlens.product.ask import run_research_ask
from eventlens.product.mvp_acceptance import audit_mvp_outcome
from tests.product.test_research_evidence import _bundle


def _actual_current_lifecycle(tmp_path: Path):
    facts = _bundle()
    run_research_ask(
        "Prepare DEMO earnings research", ticker=facts.target.ticker,
        evidence_bundle=facts, settings=EventLensSettings(),
        output_dir=tmp_path, session_id="prd-outcome",
    )
    store = ExternalAgentHandoffStore(tmp_path / "external-agent")
    paused = store.load("prd-outcome")
    exported = store.load_bundle(paused)
    raw_report = DeterministicExternalAgentFixture().interpret(exported).model_dump_json()
    validated, report = store.import_report("prd-outcome", raw_report)
    reopened = ExternalAgentHandoffStore(tmp_path / "external-agent").load("prd-outcome")
    audit = audit_mvp_outcome(
        evidence=facts, handoff=reopened, report=report,
        packet=None, qa_status=None,
        fresh_process_rehydrated=reopened == validated,
        outcome=None, approved_postmortem=None,
        first_live_packet_analyst_approved=False,
    )
    return audit


def test_current_lifecycle_is_honestly_blocked_after_validated_agent_report(tmp_path: Path) -> None:
    audit = _actual_current_lifecycle(tmp_path)
    assert not audit.release_ready
    assert audit.checks["canonical_research_evidence"]
    assert audit.checks["historical_context_gate"]
    assert audit.checks["validated_agent_report"]
    assert audit.checks["fresh_process_rehydration"]
    assert {
        "canonical_decision_packet", "independent_packet_release_qa", "post_event_outcome",
        "approved_postmortem", "first_live_packet_analyst_review",
    }.issubset(audit.blockers)


@pytest.mark.xfail(
    strict=True,
    reason="PRD outcome remains blocked until W4-W8 packet, QA, outcome, and postmortem are implemented",
)
def test_prd_outcome_is_release_ready_in_actual_product_lifecycle(tmp_path: Path) -> None:
    """Strict executable completion contract; XPASS must fail the suite."""

    assert _actual_current_lifecycle(tmp_path).release_ready
