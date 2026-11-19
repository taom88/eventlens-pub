from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from eventlens.agents.external import Capability, CanonicalMetric, ExternalEvidence, HistoricalContextRef, Route
from eventlens.agents.external_bundle import assemble_external_bundle


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def _payload(order: bool = False):
    evidence = [ExternalEvidence(source_id="s2", passage_id="p2", evidence_kind="news", typed_summary="b", available_at=NOW, content_sha256="b" * 64, rights_status="summary_permitted", retention_status="summary_only", provenance_ref="r2"), ExternalEvidence(source_id="s1", passage_id="p1", evidence_kind="filing", typed_summary="a", available_at=NOW, content_sha256="a" * 64, rights_status="summary_permitted", retention_status="summary_only", provenance_ref="r1")]
    metric = CanonicalMetric(metric_id="eps", value=Decimal("1.2"), unit="USD", period="Q3", as_of=NOW, calculation_version="v1")
    history = HistoricalContextRef(card_id="c1", event_id="BETA:2026-06-24:Q3", event_date=date(2026, 6, 24), knowledge_cutoff_at=NOW, card_sha256="c" * 64, readable_summary="prior")
    if order:
        evidence.reverse()
    warnings = ["z warning", "a warning"]
    skills = [Capability(capability_id="z_skill", version="1"), Capability(capability_id="a_skill", version="1")]
    if order:
        warnings.reverse()
        skills.reverse()
    return dict(bundle_id="b", session_id="s", event_id="BETA:2026-09-30:Q4", ticker="beta", route=Route.EXTERNAL, bundle_revision=1, built_at=NOW, information_cutoff_at=NOW, target_release_status="assumed", research_question="Prepare BETA earnings research.", canonical_metrics=[metric], evidence=evidence, historical_context=[history], warnings=warnings, allowed_skills=skills)


def test_assembly_is_deterministic_and_sorted():
    assert assemble_external_bundle(_payload()).canonical_bytes() == assemble_external_bundle(_payload(True)).canonical_bytes()


def test_assembly_rejects_unsafe_inputs():
    with pytest.raises(ValueError, match="secret"):
        assemble_external_bundle({**_payload(), "api_key": "secret"})
    with pytest.raises(ValueError, match="paths"):
        assemble_external_bundle({**_payload(), "mutable_path": "/tmp/x"})


def test_assembly_rejects_post_cutoff_evidence():
    payload = _payload()
    payload["evidence"] = (ExternalEvidence(source_id="s", passage_id="p", evidence_kind="news", typed_summary="x", available_at=NOW.replace(hour=13), content_sha256="d" * 64, rights_status="summary_permitted", retention_status="summary_only", provenance_ref="r"),)
    with pytest.raises(ValueError, match="after information cutoff"):
        assemble_external_bundle(payload)


def test_mapping_inputs_cannot_bypass_temporal_or_outcome_contracts():
    payload = _payload()
    payload["canonical_metrics"] = [
        {
            "metric_id": "actual_eps",
            "value": "2.0",
            "unit": "USD",
            "period": "Q3",
            "as_of": NOW.isoformat(),
            "calculation_version": "v1",
            "scope": "target_outcome",
        }
    ]
    with pytest.raises(ValueError):
        assemble_external_bundle(payload)


def test_rights_contract_rejects_verbatim_text_without_permission():
    payload = _payload()
    payload["evidence"] = [
        {
            "source_id": "s",
            "passage_id": "p",
            "evidence_kind": "news",
            "text": "copied article body",
            "available_at": NOW.isoformat(),
            "content_sha256": "d" * 64,
            "rights_status": "metadata_only",
            "retention_status": "headline_only",
            "provenance_ref": "r",
        }
    ]
    with pytest.raises(ValueError, match="verbatim evidence text"):
        assemble_external_bundle(payload)
