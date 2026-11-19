import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eventlens.agents import (
    CanonicalMetric,
    DeterministicExternalAgentFixture,
    ExternalEvidence,
    assemble_external_bundle,
    run_configured_agent_route,
)
from eventlens.config import EventLensSettings


NOW = datetime(2026, 10, 24, 16, tzinfo=timezone.utc)


def _bundle(route: str):
    return assemble_external_bundle(
        bundle_id=f"bundle-{route}", session_id=f"session-{route}",
        event_id="DEMO:2026-10-29:2026Q3", ticker="DEMO", route=route,
        bundle_revision=1, built_at=NOW, information_cutoff_at=NOW,
        target_release_status="assumed_after_market_close",
        research_question="Prepare Example Systems earnings research.",
        canonical_metrics=(CanonicalMetric(
            metric_id="consensus_eps", value=Decimal("2.001"), unit="USD_PER_SHARE",
            period="2026Q3", as_of=NOW, calculation_version="v1",
        ),),
        evidence=(ExternalEvidence(
            source_id="source-1", passage_id="p1", evidence_kind="issuer_release",
            typed_summary="Material demand evidence.", available_at=NOW,
            content_sha256="a" * 64, rights_status="summary_permitted",
            retention_status="summary_only", provenance_ref="receipt-1",
        ),),
    )


class _FailIfCalled:
    async def interpret(self, bundle):
        raise AssertionError("external route must not invoke an agent")


class _FixtureDeepSeek:
    async def interpret(self, bundle):
        return DeterministicExternalAgentFixture().interpret(bundle)


def test_default_external_route_pauses_without_invocation(tmp_path: Path) -> None:
    settings = EventLensSettings.load(tmp_path / "missing.env", environment={})
    state = asyncio.run(run_configured_agent_route(
        _bundle("external"), settings=settings, artifact_root=tmp_path / "external",
        deepseek=_FailIfCalled(),
    ))
    assert state.status.value == "awaiting_external_agent"
    assert state.report_id is None


def test_flag_off_deepseek_route_validates_and_persists_same_contract(tmp_path: Path) -> None:
    settings = EventLensSettings.load(
        tmp_path / "missing.env",
        environment={
            "EVENTLENS_EXTERNAL_AGENT_ENABLED": "false",
            "DEEPSEEK_API_KEY": "test-secret",
        },
    )
    state = asyncio.run(run_configured_agent_route(
        _bundle("deepseek"), settings=settings, artifact_root=tmp_path / "deepseek",
        deepseek=_FixtureDeepSeek(),
    ))
    assert state.status.value == "validated"
    assert state.route.value == "deepseek"
    assert state.report_id is not None
