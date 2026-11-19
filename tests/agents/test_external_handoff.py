import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eventlens.agents import (
    CanonicalMetric,
    ExternalAgentHandoffStore,
    ExternalAgentReport,
    ExternalClaim,
    ExternalEvidence,
    ExternalRecommendation,
    ExternalReportIssueCode,
    ExternalReportValidationError,
    ExternalScenario,
    UsageReceipt,
    assemble_external_bundle,
    evidence_id,
)
from eventlens.agents.models import Citation


NOW = datetime(2026, 10, 24, 16, tzinfo=timezone.utc)


def _bundle():
    return assemble_external_bundle(
        bundle_id="bundle-1",
        session_id="session-1",
        event_id="DEMO:2026-10-29:2026Q3",
        ticker="DEMO",
        route="external",
        bundle_revision=1,
        built_at=NOW,
        information_cutoff_at=NOW,
        target_release_status="assumed_after_market_close",
        research_question="Prepare Example Systems earnings research.",
        canonical_metrics=(
            CanonicalMetric(
                metric_id="consensus_eps",
                value=Decimal("2.001"),
                unit="USD_PER_SHARE",
                period="2026Q3",
                as_of=NOW,
                calculation_version="v1",
            ),
        ),
        evidence=(
            ExternalEvidence(
                source_id="source-1",
                passage_id="p1",
                evidence_kind="issuer_release",
                typed_summary="cloud demand remained material.",
                available_at=NOW,
                content_sha256="a" * 64,
                rights_status="summary_permitted",
                retention_status="summary_only",
                provenance_ref="provider-receipt-1",
            ),
        ),
    )


def _report(bundle=None):
    bundle = bundle or _bundle()
    citation = Citation(source_id="source-1", passage_id="p1")
    return ExternalAgentReport(
        report_id="report-1",
        bundle_id=bundle.bundle_id,
        bundle_revision=bundle.bundle_revision,
        bundle_sha256=bundle.content_sha256,
        session_id=bundle.session_id,
        event_id=bundle.event_id,
        generated_at=NOW,
        claims=(
            ExternalClaim(
                claim_id="claim-1",
                text="Demand is a material debate.",
                citations=(citation,),
                confidence=Decimal("0.7"),
            ),
        ),
        scenarios=(
            ExternalScenario(
                scenario_id="bull", thesis="Upside", probability=Decimal("0.25"),
                canonical_metric_ids=("consensus_eps",), citations=(citation,),
            ),
            ExternalScenario(
                scenario_id="base", thesis="Balanced", probability=Decimal("0.5"),
                citations=(citation,),
            ),
            ExternalScenario(
                scenario_id="bear", thesis="Downside", probability=Decimal("0.25"),
                citations=(citation,),
            ),
        ),
        recommendation=ExternalRecommendation(
            action="HOLD",
            conviction=Decimal("0.55"),
            thesis="Risk and reward are balanced.",
            counter_thesis="cloud acceleration could surprise.",
            supporting_evidence_ids=(evidence_id("source-1", "p1"),),
        ),
        usage=UsageReceipt(
            provider="fixture",
            model="fixture-agent-v1",
            prompt_version="external-agent-v1",
            started_at=NOW,
            completed_at=NOW,
        ),
    )


def test_export_pause_import_and_idempotent_reimport(tmp_path):
    bundle = _bundle()
    store = ExternalAgentHandoffStore(tmp_path)

    waiting = store.export(bundle)
    assert waiting.status.value == "awaiting_external_agent"
    assert store.export(bundle) == waiting

    report = _report(bundle)
    validated, restored = store.import_report(
        bundle.session_id, report.model_dump_json()
    )
    assert validated.status.value == "validated"
    assert restored == report
    assert validated.report_markdown_path is not None
    assert "**HOLD**" in __import__("pathlib").Path(validated.report_markdown_path).read_text()
    assert store.import_report(bundle.session_id, report.model_dump_json())[0] == validated


def test_rejected_import_does_not_write_validated_report(tmp_path):
    bundle = _bundle()
    store = ExternalAgentHandoffStore(tmp_path)
    waiting = store.export(bundle)
    payload = _report(bundle).model_dump(mode="json")
    payload["bundle_sha256"] = "b" * 64

    with pytest.raises(ExternalReportValidationError) as caught:
        store.import_report(bundle.session_id, json.dumps(payload))

    assert caught.value.code is ExternalReportIssueCode.BINDING_MISMATCH
    assert store.load(bundle.session_id) == waiting
    assert not list((tmp_path / bundle.session_id).glob("report-*.json"))


def test_duplicate_json_keys_fail_before_schema_validation(tmp_path):
    store = ExternalAgentHandoffStore(tmp_path)
    store.export(_bundle())
    with pytest.raises(ExternalReportValidationError) as caught:
        store.import_report("session-1", '{"report_id":"one","report_id":"two"}')
    assert caught.value.code is ExternalReportIssueCode.DUPLICATE_JSON_KEY


def test_concurrent_different_imports_compare_and_set_one_report(tmp_path):
    bundle = _bundle()
    store = ExternalAgentHandoffStore(tmp_path)
    store.export(bundle)
    first = _report(bundle)
    second = first.model_copy(update={"report_id": "report-2"})

    def attempt(report):
        try:
            return store.import_report(bundle.session_id, report.model_dump_json())[0].report_id
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (first, second)))

    assert results.count("conflict") == 1
    assert store.load(bundle.session_id).report_id in {"report-1", "report-2"}
