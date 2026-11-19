import json
from pathlib import Path

from eventlens.agents import (
    DeterministicExternalAgentFixture,
    ExternalAgentHandoffStore,
)
from eventlens.app.commands import main
from eventlens.memory import RunRecordKind
from eventlens.product.research_evidence import ResearchEvidenceStore
from eventlens.web.product_session import ProductSessionFacade, _product_run_id
from tests.product.test_research_evidence import _bundle


def test_product_cli_persists_pause_and_validated_import_in_one_repository_path(
    tmp_path: Path, capsys,
) -> None:
    evidence_path = tmp_path / "research-evidence.json"
    ResearchEvidenceStore().write(_bundle(), evidence_path)
    product_root = tmp_path / "product-sessions"
    env_path = tmp_path / "missing.env"
    session_id = "cli-product-session"

    assert main(
        [
            "--env-file", str(env_path),
            "research-ask",
            "--question", "Prepare DEMO earnings research",
            "--ticker", "DEMO",
            "--evidence-bundle", str(evidence_path),
            "--output-dir", str(product_root),
            "--session-id", session_id,
        ]
    ) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["status"] == "awaiting_external_agent"
    assert prepared["ticker"] == "DEMO"

    handoff_store = ExternalAgentHandoffStore(
        product_root / "live-artifacts" / "external-agent"
    )
    external_bundle = handoff_store.load_bundle(handoff_store.load(session_id))
    report = DeterministicExternalAgentFixture().interpret(external_bundle)
    report_path = tmp_path / "external-report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    assert main(
        [
            "--env-file", str(env_path),
            "research-import-agent-report",
            "--session-id", session_id,
            "--report", str(report_path),
            "--output-dir", str(product_root),
        ]
    ) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["status"] == "validated"
    assert imported["validated_report_id"] == report.report_id

    facade = ProductSessionFacade(session_root=product_root)
    reopened = facade.load(session_id)
    assert reopened.status == "validated"
    run_id = _product_run_id(
        session_id,
        owner_id=facade.settings.memory_owner_id,
        workspace_id=facade.settings.memory_workspace_id,
    )
    revisions = facade.repository.list_run_records(
        facade.settings.memory_workspace_id,
        run_id,
        kind=RunRecordKind.TRANSITION,
    )
    assert revisions[-1].metadata["product_session"]["status"] == "validated"


def test_product_cli_fails_closed_when_selected_supabase_is_not_configured(
    tmp_path: Path, capsys,
) -> None:
    evidence_path = tmp_path / "research-evidence.json"
    ResearchEvidenceStore().write(_bundle(), evidence_path)
    env_path = tmp_path / ".env"
    env_path.write_text("EVENTLENS_MEMORY_BACKEND=supabase\n", encoding="utf-8")
    product_root = tmp_path / "must-not-be-created"

    assert main(
        [
            "--env-file", str(env_path),
            "research-ask",
            "--question", "Prepare DEMO earnings research",
            "--evidence-bundle", str(evidence_path),
            "--output-dir", str(product_root),
            "--session-id", "supabase-misconfigured",
        ]
    ) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["error"] == "MemoryRepositoryError"
    assert "Supabase" in failure["detail"]
    assert not product_root.exists()
