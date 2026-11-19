from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from eventlens.artifacts.models import sha256_hex
from eventlens.config import AgentRoute
from eventlens.context_cards import ContextCardV4
from eventlens.context_cards.guidance import GuidanceEntry, GuidanceType, GuidanceValue
from eventlens.context_cards.metrics import (
    AccountingBasis, FiscalPeriod, GrowthBasis, MetricScope, MetricUnit,
    MetricValue, NormalizedMetric,
)
from eventlens.context_cards.models import ConsensusState
from eventlens.product.research_evidence import (
    ConsensusEvidence, EvidenceReceipt, HistoricalAnalyticsEvidence,
    ManagementEvidence, PriorContextCardEvidence, ResearchEvidenceBundle, TechnicalEvidence,
    assemble_research_evidence_bundle,
)
from eventlens.product.synthesis_input import external_bundle_from_research_evidence
from eventlens.product.news_signal import aggregate_news_signal
from eventlens.providers.content import ContentRetentionPolicy
from eventlens.providers.fixture import FixtureProvider
from eventlens.quant.backtest import event_study_report_sha256
from eventlens.quant.inputs import assemble_event_analytics_inputs, run_event_study_from_bundle
from eventlens.quant.models import Market, US_MARKET_ASSUMPTIONS
from eventlens.quant.pre_event_features import PreEventFeatureSnapshot
from eventlens.quant.supplement import (
    BenchmarkEventObservation, CostScenarioPolicy, build_event_study_supplement,
)

from tests.context_cards.test_schema import _minimal_card

from .test_research_evidence import _bundle, _evidence_for_news_signal, _news_evidence


def _populated_bundle():
    from tests.agents.test_news import CUTOFF as news_cutoff

    cutoff = news_cutoff
    base = _bundle(cutoff=cutoff, news=_news_evidence(cutoff=cutoff))

    card_payload = _minimal_card()
    card_payload["identity"]["card"]["status"] = "approved"
    card_payload["identity"]["card"]["generated_at"] = "2026-06-02T00:00:00+00:00"
    card_payload["identity"]["card"]["as_of"] = "2026-06-02T00:00:00+00:00"
    card_payload["identity"]["event"] = {
        "type": "earnings", "fiscal_period": "FY2026Q2",
        "event_datetime": "2026-06-01T20:00:00+00:00", "timezone": "UTC",
        "event_datetime_status": "confirmed",
    }
    card_payload["temporal_state"]["historical_event_cutoff"] = "2026-05-31T20:00:00+00:00"
    card_payload["provenance"]["generated_at"] = "2026-06-02T00:00:00+00:00"
    card = ContextCardV4.model_validate(card_payload)
    card_sha = sha256_hex(card.model_dump(mode="json"))
    prior = PriorContextCardEvidence(
        prior_event_id="DEMO:2026-06-01:2026Q2", card_id="demo-approved-prior",
        event_date=datetime(2026, 6, 1, tzinfo=timezone.utc).date(),
        knowledge_cutoff_at=card.identity.card.as_of, card=card, card_sha256=card_sha,
        receipt=EvidenceReceipt(
            component="prior_context_card", receipt_id="prior:demo-approved",
            content_sha256=card_sha, available_at=card.identity.card.as_of,
            source_ref="eventlens-context-card:demo-approved-prior",
        ),
    )

    period = FiscalPeriod(fiscal_year=2026, fiscal_quarter=3)
    consensus_state = ConsensusState(
        status="available", fiscal_period="FY2026Q3",
        metrics={NormalizedMetric.REVENUE: MetricValue(
            metric=NormalizedMetric.REVENUE, value=Decimal("42.5"),
            unit=MetricUnit.USD_BILLION, fiscal_period=period,
            accounting_basis=AccountingBasis.GAAP,
            growth_basis=GrowthBasis.NOT_APPLICABLE,
            scope=MetricScope.CONSOLIDATED,
        )},
    )
    consensus_sha = sha256_hex(consensus_state.model_dump(mode="json"))
    consensus = ConsensusEvidence(
        consensus=consensus_state, availability_status="available",
        evidence_refs=("consensus:demo",), consensus_sha256=consensus_sha,
        receipt=EvidenceReceipt(
            component="consensus", receipt_id="consensus:demo-receipt",
            content_sha256=consensus_sha, available_at=cutoff.replace(hour=15),
            source_ref="consensus:demo", provenance_refs=("consensus:demo",),
        ),
    )

    guidance = GuidanceEntry(
        guidance_id="guidance-revenue-q3", issued_fiscal_period=FiscalPeriod(
            fiscal_year=2026, fiscal_quarter=2,
        ), target_fiscal_period=period, metric=NormalizedMetric.REVENUE,
        guidance=GuidanceValue(
            type=GuidanceType.RANGE, low=Decimal("41"), high=Decimal("43"),
            unit=MetricUnit.USD_BILLION, accounting_basis=AccountingBasis.GAAP,
        ), eventual_actual_missing_reason="target event has not occurred",
        evidence_refs=("issuer-guidance:demo",),
    )
    guidance_sha = sha256_hex({
        "guidance": [guidance.model_dump(mode="json")],
        "statements": [
            item.model_dump(mode="json")
            for item in base.company_context.context.management
        ],
    })
    management = ManagementEvidence(
        guidance=(guidance,), statements=base.company_context.context.management,
        guidance_sha256=guidance_sha,
        receipt=EvidenceReceipt(
            component="management_guidance", receipt_id="guidance:demo-receipt",
            content_sha256=guidance_sha, available_at=cutoff.replace(hour=14),
            source_ref="issuer-guidance:demo",
            provenance_refs=("issuer-guidance:demo",),
        ),
    )

    snapshot = PreEventFeatureSnapshot(
        ticker="DEMO", event_id=base.target.event_id, event_date=base.target.event_date,
        cutoff_at=cutoff, article_count=1, source_count=1,
        topic_counts={"earnings": 1}, weighted_sentiment=None,
        latest_close=Decimal("101.25"), prior_5d_return_pct=Decimal("1.5"),
        prior_20d_return_pct=Decimal("4.0"),
    )
    technical_sha = sha256_hex(snapshot.model_dump(mode="json"))
    technical = TechnicalEvidence.from_snapshot(
        snapshot,
        receipt=EvidenceReceipt(
            component="pre_event_features", receipt_id="technical:demo",
            content_sha256=technical_sha, available_at=cutoff.replace(hour=15),
            source_ref="market:demo",
        ),
    )

    provider = FixtureProvider(Path("fixtures"))
    analytics_bundle = assemble_event_analytics_inputs(
        provider.get_earnings_events("DEMO", datetime(2020, 1, 1).date(), datetime(2030, 1, 1).date()),
        provider.get_daily_history("DEMO", datetime(2020, 1, 1).date(), datetime(2030, 1, 1).date()),
        information_cutoff_at=datetime(2026, 1, 6, 21, tzinfo=timezone.utc),
        allow_degraded=True,
    )
    report, manifest = run_event_study_from_bundle(analytics_bundle)
    benchmark = tuple(BenchmarkEventObservation(
        event_id=item.event_id, event_date=item.event_date,
        t_plus_1_return_pct=Decimal("0.01"), t_plus_5_return_pct=Decimal("0.02"),
    ) for item in report.observations)
    supplement = build_event_study_supplement(
        report, benchmark, benchmark_id="QQQ", benchmark_market=Market.US,
        cost_policy=CostScenarioPolicy(
            market_assumptions=US_MARKET_ASSUMPTIONS, turnover=Decimal("2"),
            policy_version="us-round-trip-v1",
        ),
    )
    manifest_sha = sha256_hex(manifest.model_dump(mode="json"))
    report_sha = event_study_report_sha256(report)
    supplement_sha = sha256_hex(supplement.model_dump(mode="json"))
    analytics = HistoricalAnalyticsEvidence(
        input_manifest=manifest, event_study=report, supplement=supplement,
        manifest_sha256=manifest_sha, report_sha256=report_sha,
        supplement_sha256=supplement_sha,
        receipt=EvidenceReceipt(
            component="historical_analytics", receipt_id="analytics:demo",
            content_sha256=sha256_hex({
                "manifest": manifest_sha, "report": report_sha,
                "supplement": supplement_sha,
            }),
            available_at=cutoff.replace(hour=13), source_ref="analytics:demo",
            quality="degraded",
        ),
    )
    return assemble_research_evidence_bundle(
        bundle_id="populated-demo", target=base.target,
        information_cutoff_at=cutoff, assembled_at=cutoff,
        company_context=base.company_context, prior_cards=(prior,),
        consensus=consensus, management=management, news=base.news,
        technical=technical, historical_analytics=analytics,
    )


def test_research_evidence_is_the_only_universal_synthesis_input() -> None:
    facts = _bundle()
    exported = external_bundle_from_research_evidence(
        facts,
        question="Prepare issuer earnings research",
        session_id="issuer-session",
        route=AgentRoute.EXTERNAL,
        built_at=datetime(2026, 10, 24, 21, tzinfo=timezone.utc),
    )

    assert exported.ticker == facts.target.ticker
    assert exported.event_id == facts.target.event_id
    assert exported.information_cutoff_at == facts.information_cutoff_at
    assert exported.evidence
    assert all(item.available_at < exported.information_cutoff_at for item in exported.evidence)
    assert "prior_cards" in exported.unavailable_stages
    serialized = exported.model_dump_json()
    assert "api_key" not in serialized
    assert "post_event" not in serialized


def test_projection_is_ticker_agnostic_and_deterministic() -> None:
    facts = _bundle()
    kwargs = dict(
        question="Prepare issuer earnings research",
        session_id="issuer-session",
        route=AgentRoute.EXTERNAL,
        built_at=datetime(2026, 10, 24, 21, tzinfo=timezone.utc),
    )
    first = external_bundle_from_research_evidence(facts, **kwargs)
    second = external_bundle_from_research_evidence(facts, **kwargs)
    assert first.canonical_bytes() == second.canonical_bytes()


def test_populated_production_projection_preserves_every_component(monkeypatch) -> None:
    facts = _populated_bundle()
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )
    kwargs = dict(
        question="Prepare populated issuer earnings research",
        session_id="populated-session", route=AgentRoute.EXTERNAL,
        built_at=datetime(2026, 10, 24, 21, tzinfo=timezone.utc),
    )
    first = external_bundle_from_research_evidence(facts, **kwargs)
    second = external_bundle_from_research_evidence(facts, **kwargs)

    metric_ids = {item.metric_id for item in first.canonical_metrics}
    evidence_kinds = {item.evidence_kind for item in first.evidence}
    assert {
        "ontology.critical_slots_populated",
        "consensus.revenue",
        "management_guidance.guidance-revenue-q3.midpoint",
        "news.weighted_content_score",
        "technical.latest_close",
        "historical_analytics.event_count",
        "historical_analytics.next_open.mean_pct",
    } <= metric_ids
    assert {
        "target.identity",
        "company_context.management",
        "ontology.coverage",
        "consensus.state",
        "consensus.metric",
        "management.guidance",
        "news.aggregate",
        "news.item",
        "news.classification",
        "news.selected_passage",
        "news.claim",
        "historical_analytics.manifest",
        "historical_analytics.observation",
        "historical_analytics.summary",
        "historical_analytics.supplement.metadata",
        "historical_analytics.supplement.benchmark",
        "historical_analytics.supplement.t_plus_1_costs",
        "historical_analytics.supplement.t_plus_5_costs",
    } <= evidence_kinds
    assert tuple(item.card_id for item in first.historical_context) == (
        "demo-approved-prior",
    )
    consensus_metric = next(
        item for item in first.evidence if item.evidence_kind == "consensus.metric"
    )
    consensus_payload = json.loads(consensus_metric.typed_summary or "{}")
    assert consensus_payload["accounting_basis"] == "gaap"
    assert consensus_payload["growth_basis"] == "not_applicable"
    assert consensus_payload["scope"] == "consolidated"
    claim = next(item for item in first.evidence if item.evidence_kind == "news.claim")
    available_ids = {item.passage_id for item in first.evidence}
    assert set(json.loads(claim.typed_summary or "{}")["projected_citation_ids"]) <= available_ids
    assert first.unavailable_stages == ()
    assert all(item.available_at <= first.information_cutoff_at for item in first.evidence)
    assert first.canonical_bytes() == second.canonical_bytes()


@pytest.mark.parametrize(
    ("retention", "summary", "expected_rights", "expected_retention", "content_status"),
    (
        (ContentRetentionPolicy.METADATA_ONLY, False, "metadata_only", "headline_only", "headline_only"),
        (ContentRetentionPolicy.METADATA_ONLY, True, "summary_permitted", "summary_only", "summary_only"),
    ),
)
def test_news_projection_preserves_degraded_rights_and_resolves_claim_citations(
    retention, summary, expected_rights, expected_retention, content_status,
) -> None:
    from tests.agents.test_news import CUTOFF as news_cutoff, _request, _result

    result = _result(_request(retention, summary=summary))
    signal = aggregate_news_signal((result,), cutoff_at=news_cutoff)
    news = _evidence_for_news_signal(signal, cutoff=news_cutoff)
    populated = _populated_bundle()
    facts = ResearchEvidenceBundle.model_validate(
        populated.model_copy(update={"news": news, "bundle_sha256": None}).model_dump(mode="json")
    )
    exported = external_bundle_from_research_evidence(
        facts, question="Preserve rights", session_id=f"rights-{summary}",
        route=AgentRoute.EXTERNAL,
        built_at=datetime(2026, 10, 24, 21, tzinfo=timezone.utc),
    )
    classification = next(
        item for item in exported.evidence if item.evidence_kind == "news.classification"
    )
    assert classification.rights_status.value == expected_rights
    assert classification.retention_status.value == expected_retention
    assert json.loads(classification.typed_summary or "{}")["content_status"] == content_status
    assert any(item.evidence_kind == "news.provider_summary" for item in exported.evidence) is summary

    if result.cited_claims:
        claim = next(item for item in exported.evidence if item.evidence_kind == "news.claim")
        citation_ids = json.loads(claim.typed_summary or "{}")["projected_citation_ids"]
        available_ids = {item.passage_id for item in exported.evidence}
        assert set(citation_ids) <= available_ids


def test_projector_revalidation_rejects_forged_company_context_and_late_receipt() -> None:
    facts = _bundle()
    context = facts.company_context.context.model_copy(update={"ticker": "OTHER"})
    context_checksum = sha256_hex(context.model_dump(mode="json", exclude={"content_sha256"}))
    context = context.model_copy(update={"content_sha256": context_checksum})
    forged_company_context = facts.company_context.model_copy(update={
        "context": context,
        "receipt": facts.company_context.receipt.model_copy(
            update={"content_sha256": context_checksum}
        ),
    })
    forged_ticker = facts.model_copy(update={
        "company_context": forged_company_context,
        "bundle_sha256": None,
    })
    late_receipt = facts.model_copy(update={
        "company_context": facts.company_context.model_copy(update={
            "receipt": facts.company_context.receipt.model_copy(
                update={"available_at": facts.information_cutoff_at}
            ),
        }),
        "bundle_sha256": None,
    })
    kwargs = dict(
        question="Prepare issuer earnings research",
        session_id="issuer-session",
        route=AgentRoute.EXTERNAL,
        built_at=datetime(2026, 10, 24, 21, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="company context ticker and cutoff must match"):
        external_bundle_from_research_evidence(forged_ticker, **kwargs)
    with pytest.raises(ValueError, match="evidence receipts must be strictly before"):
        external_bundle_from_research_evidence(late_receipt, **kwargs)


def test_prior_cards_require_approval_and_history_exception_requires_typed_eligibility() -> None:
    populated = _populated_bundle()
    prior = populated.prior_cards[0]
    draft_card = prior.card.model_copy(update={
        "identity": prior.card.identity.model_copy(update={
            "card": prior.card.identity.card.model_copy(update={"status": "draft"}),
        }),
    })
    draft_sha = sha256_hex(draft_card.model_dump(mode="json"))
    draft_prior = prior.model_copy(update={
        "card": draft_card, "card_sha256": draft_sha,
        "receipt": prior.receipt.model_copy(update={"content_sha256": draft_sha}),
    })
    with pytest.raises(ValueError, match="prior context cards must be approved"):
        ResearchEvidenceBundle.model_validate(
            populated.model_copy(update={
                "prior_cards": (draft_prior,), "bundle_sha256": None,
            }).model_dump(mode="json")
        )

    newly_covered = _bundle()
    with pytest.raises(ValueError, match="explicit newly-covered issuer eligibility"):
        ResearchEvidenceBundle.model_validate(
            newly_covered.model_copy(update={
                "newly_covered_issuer": None, "bundle_sha256": None,
            }).model_dump(mode="json")
        )
