from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from eventlens.agents import CanonicalMetric, ExternalAgentBundle, ExternalAgentReport, ExternalEvidence, ExternalRecommendation, ExternalScenario, ReactionForecast, UsageReceipt, external_agent_json_schema
from eventlens.agents.models import Citation

NOW = datetime(2026, 1, 2, 15, tzinfo=timezone.utc)
SHA = "a" * 64

def metric(metric_id="eps"):
    return CanonicalMetric(metric_id=metric_id, value=Decimal("1.20"), unit="USD", period="FY26", as_of=NOW, calculation_version="v1")

def evidence(passage="p1", available=NOW):
    return ExternalEvidence(source_id="s1", passage_id=passage, evidence_kind="filing", text="bounded", available_at=available, content_sha256=SHA, rights_status="retention_permitted", retention_status="bounded_excerpt", provenance_ref="prov-1")

def bundle(**overrides):
    values = dict(bundle_id="b1", session_id="s1", event_id="e1", ticker="demo", route="external", bundle_revision=1, built_at=NOW, information_cutoff_at=NOW, target_release_status="confirmed", research_question="Prepare earnings research.", canonical_metrics=(metric(),), evidence=(evidence(),))
    values.update(overrides)
    return ExternalAgentBundle.create(**values)

def usage():
    return UsageReceipt(provider="fixture", model="test", prompt_version="v1", started_at=NOW, completed_at=NOW)

def report(**overrides):
    c = Citation(source_id="s1", passage_id="p1")
    values = dict(report_id="r1", bundle_id="b1", bundle_revision=1, bundle_sha256=bundle().content_sha256, session_id="s1", event_id="e1", generated_at=NOW, scenarios=(ExternalScenario(scenario_id="bull", thesis="up", probability=Decimal(".2"), citations=(c,)), ExternalScenario(scenario_id="base", thesis="flat", probability=Decimal(".5")), ExternalScenario(scenario_id="bear", thesis="down", probability=Decimal(".3"))), recommendation=ExternalRecommendation(action="DONT_BUY", conviction=Decimal(".5"), thesis="t", counter_thesis="ct"), usage=usage())
    values.update(overrides)
    return ExternalAgentReport(**values)

def test_roundtrip_decimal_time_and_ascii_action():
    b = bundle()
    assert ExternalAgentBundle.model_validate_json(b.model_dump_json()).canonical_bytes() == b.canonical_bytes()
    assert report().model_dump(mode="json")["recommendation"]["action"] == "DONT_BUY"

def test_schema_version_extras_duplicates_and_cutoff_fail_closed():
    with pytest.raises(ValidationError): ExternalAgentBundle.model_validate({**bundle().model_dump(), "schema_version": "2.0"})
    with pytest.raises(ValidationError): ExternalAgentBundle.model_validate({**bundle().model_dump(), "unknown": True})
    with pytest.raises(ValidationError, match="metric"): bundle(canonical_metrics=(metric(), metric()))
    with pytest.raises(ValidationError, match="available"): bundle(evidence=(evidence(available=NOW.replace(hour=16)),))

def test_checksum_stability_and_permutation():
    assert bundle().content_sha256 == bundle().content_sha256
    assert bundle(canonical_metrics=(metric("z"), metric("a"))).content_sha256 != bundle(canonical_metrics=(metric("a"), metric("z"))).content_sha256
    with pytest.raises(ValidationError, match="checksum"):
        ExternalAgentBundle.model_validate(
            {**bundle().model_dump(mode="json"), "content_sha256": "0" * 64}
        )

def test_forecast_probability_invariants():
    with pytest.raises(ValidationError): ReactionForecast(signed_move_pct=Decimal("-.2"), absolute_move_pct=Decimal(".1"), horizon="T+1", uncertainty="high", tradeability="low")
    with pytest.raises(ValidationError, match="probabilities"): report(scenarios=(ExternalScenario(scenario_id="bull", thesis="up", probability=Decimal(".1")), ExternalScenario(scenario_id="base", thesis="flat", probability=Decimal(".1")), ExternalScenario(scenario_id="bear", thesis="down", probability=Decimal(".1"))))

def test_checked_in_schema_matches_models():
    path = Path(__file__).parents[2] / "schemas" / "eventlens.external_agent.v1.schema.json"
    assert json.loads(path.read_text()) == external_agent_json_schema()
