from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eventlens.agents import (
    CanonicalMetric,
    ExternalEvidence,
    assemble_external_bundle,
)
from eventlens.config import AgentRoute, EventLensSettings


NOW = datetime(2026, 10, 24, 16, tzinfo=timezone.utc)


def _assemble(settings: EventLensSettings):
    return assemble_external_bundle(
        bundle_id="bundle-1",
        session_id="session-1",
        event_id="DEMO:2026-10-29:2026Q3",
        ticker="DEMO",
        route=settings.agent_route(),
        bundle_revision=1,
        built_at=NOW,
        information_cutoff_at=NOW,
        target_release_at=datetime(2026, 10, 29, 20, tzinfo=timezone.utc),
        target_release_status="assumed_after_market_close",
        research_question="Prepare Example Systems earnings research.",
        canonical_metrics=(
            CanonicalMetric(
                metric_id="consensus_eps",
                value=Decimal("2.001"),
                unit="USD_PER_SHARE",
                period="2026Q3",
                as_of=NOW,
                calculation_version="provider-normalization-v1",
                provenance_ids=("finnhub-calendar",),
            ),
        ),
        evidence=(
            ExternalEvidence(
                source_id="eventlens-context-card:demo-context-2026-07-30",
                passage_id="executive-summary",
                evidence_kind="historical_context",
                typed_summary="Prior event summary.",
                available_at=NOW,
                content_sha256="a" * 64,
                rights_status="summary_permitted",
                retention_status="summary_only",
                provenance_ref="demo-context-2026-07-30",
            ),
        ),
        forbidden_behaviors=("use post-cutoff evidence", "change canonical metrics"),
    )


def test_default_external_route_builds_repeatable_bundle_without_model_key(
    tmp_path: Path,
) -> None:
    settings = EventLensSettings.load(tmp_path / "missing.env", environment={})

    assert settings.agent_route() is AgentRoute.EXTERNAL
    assert settings.agent_route_readiness().ready is True
    first = _assemble(settings)
    second = _assemble(settings)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.content_sha256 == first.content_checksum()


def test_flag_off_selects_deepseek_but_uses_identical_bundle_contract(
    tmp_path: Path,
) -> None:
    settings = EventLensSettings.load(
        tmp_path / "missing.env",
        environment={
            "EVENTLENS_EXTERNAL_AGENT_ENABLED": "false",
            "DEEPSEEK_API_KEY": "test-only-secret",
        },
    )

    bundle = _assemble(settings)

    assert settings.agent_route() is AgentRoute.DEEPSEEK
    assert settings.agent_route_readiness().ready is True
    assert bundle.route is AgentRoute.DEEPSEEK
    assert "test-only-secret" not in bundle.canonical_bytes().decode("utf-8")
