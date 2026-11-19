from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eventlens.context_cards.builders import (
    HistoricalCardBuildInput,
    HistoricalCardNarrative,
    HistoricalContextCardArtifact,
    HistoricalNewsEvidenceBinding,
    ManagementGuidanceAvailability,
    MissingValue,
    NativeNewsLabelSummary,
    ReconstructedEpsContext,
    approve_historical_context_card,
    bind_historical_news_evidence,
    build_historical_context_card,
    build_historical_provider_summary_pulls,
    build_historical_semantic_reaction_bridge,
    evaluate_post_event_explanation_gate,
    revise_historical_guidance_availability,
    write_historical_context_card_artifacts,
)
from eventlens.context_cards.metrics import (
    AccountingBasis,
    FiscalPeriod,
    GrowthBasis,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
)
from eventlens.context_cards.reactions import calculate_market_reaction
from eventlens.domain.events import EarningsEvent, EventTiming, resolve_event_window
from eventlens.domain.returns import PriceObservation
from eventlens.product.live_news_evidence import (
    ProviderSummaryPhase,
    ProviderSummaryReference,
    ProviderSummarySelectionReason,
    build_news_evidence_manifest,
    select_provider_summary_references,
)
from eventlens.providers.models import NewsArticleMetadata, NewsSummaryStatus
from eventlens.quant.pre_event_features import PreEventFeatureSnapshot
from eventlens.web.context_baseline import load_context_cards

EVENT_AT = datetime(2026, 8, 10, 21, tzinfo=UTC)
HISTORICAL_CUTOFF = datetime(2026, 8, 10, 19, 30, tzinfo=UTC)
GENERATED_AT = datetime(2026, 11, 8, 20, tzinfo=UTC)
TARGET_CUTOFF = datetime(2026, 11, 9, 20, 30, tzinfo=UTC)


def _reaction(*, with_after_hours: bool = False):
    days = tuple(date(2026, 8, day) for day in (10, 11, 12, 13, 14, 17))
    window = resolve_event_window(
        EarningsEvent(
            ticker="DEMO", event_date=days[0], timing=EventTiming.AFTER_MARKET_CLOSE
        ),
        days,
    )
    observations = tuple(
        PriceObservation(
            trading_date=day, open=Decimal(100 + index), close=Decimal(101 + index)
        )
        for index, day in enumerate(days)
    )
    benchmark = tuple(
        PriceObservation(
            trading_date=day, open=Decimal(200 + index), close=Decimal(201 + index)
        )
        for index, day in enumerate(days)
    )
    return calculate_market_reaction(
        observations[0],
        observations[1:],
        event_window=window,
        fiscal_period="FY2026_Q2",
        after_hours_price=Decimal(99) if with_after_hours else None,
        benchmark_reference=benchmark[0],
        benchmark_post_event_sessions=benchmark[1:],
        benchmark_after_hours_price=Decimal(199) if with_after_hours else None,
        benchmark_ticker="SPY",
    )


def _input(**changes) -> HistoricalCardBuildInput:
    period = FiscalPeriod(
        fiscal_year=2026, fiscal_quarter=2, period_end=date(2026, 6, 30)
    )
    values = {
        "card_id": "demo-fy2026-q2-2026-08-10-v1",
        "event_id": "DEMO:2026-08-10:FY2026Q2",
        "ticker": "DEMO",
        "company": "Example Systems, Inc.",
        "fiscal_period": period,
        "event_at": EVENT_AT,
        "historical_event_cutoff_at": HISTORICAL_CUTOFF,
        "target_cutoff_at": TARGET_CUTOFF,
        "generated_at": GENERATED_AT,
        "actual_metrics": (
            MetricValue(
                metric=NormalizedMetric.REVENUE,
                value=Decimal("123.456"),
                unit=MetricUnit.USD_MILLION,
                fiscal_period=period,
                accounting_basis=AccountingBasis.GAAP,
                growth_basis=GrowthBasis.NOT_APPLICABLE,
                scope=MetricScope.CONSOLIDATED,
            ),
        ),
        "issuer_source_url": "https://example.com/issuer/demo-q2",
        "issuer_source_claim": "The issuer reported second-quarter results.",
        "issuer_source_sha256": "a" * 64,
        "issuer_source_retrieved_at": GENERATED_AT - timedelta(minutes=1),
        "market_source_sha256": "b" * 64,
        "benchmark_ticker": "SPY",
        "benchmark_source_sha256": "c" * 64,
        "pre_event_features": PreEventFeatureSnapshot(
            ticker="DEMO",
            event_id="DEMO:2026-08-10:FY2026Q2",
            event_date=EVENT_AT.date(),
            cutoff_at=HISTORICAL_CUTOFF,
            article_count=0,
            source_count=0,
            topic_counts={},
            latest_close=Decimal(48),
            prior_5d_return_pct=Decimal("0.03"),
            prior_20d_return_pct=Decimal("0.12"),
        ),
        "pre_event_missing_values": (
            MissingValue(
                field="weighted_sentiment",
                reason="No authorized news provider was configured.",
            ),
        ),
        "prior_20d_average_volume": Decimal(20000000),
        "reference_session_volume_vs_prior_20d": Decimal("1.5"),
        "reaction": _reaction(),
        "narrative": HistoricalCardNarrative(
            executive_summary="Revenue grew while profitability remained mixed.",
            key_messages=("Revenue increased.", "The reaction is descriptive."),
            metric_interpretation="GAAP revenue is kept separate from non-GAAP measures.",
            reaction_interpretation="The next-session response is factual, not causal.",
            caveats=("Historical sell-side consensus was not collected.",),
        ),
    }
    values.update(changes)
    return HistoricalCardBuildInput(**values)


def test_builder_emits_reviewed_readable_card_without_pre_event_outcome_leakage() -> (
    None
):
    artifact = build_historical_context_card(_input())

    assert artifact.status == "reviewed"
    assert artifact.canonical_card.quality_control.requires_human_review is True
    assert artifact.canonical_card.current_pre_event_state.latest_actuals == {}
    assert artifact.canonical_card.historical_quarters[0].actual_results.metrics[
        NormalizedMetric.REVENUE
    ].value == Decimal("123.456")
    assert artifact.pre_event_features["weighted_sentiment"] is None
    assert artifact.reaction_metrics["after_hours_return_pct"] is None
    assert artifact.management_guidance_availability is not None
    assert artifact.management_guidance_availability.status == "unavailable"
    assert artifact.citation_uri == (
        "eventlens-context-card:demo-fy2026-q2-2026-08-10-v1"
    )
    with pytest.raises(ValueError, match="explicitly approved"):
        artifact.as_prior_evidence()


def test_guidance_availability_is_typed_and_legacy_revision_is_immutable(
    tmp_path,
) -> None:
    artifact = build_historical_context_card(_input())
    legacy = HistoricalContextCardArtifact.model_validate(
        artifact.model_dump(exclude={"management_guidance_availability"})
    )
    assert legacy.management_guidance_availability is None

    availability = ManagementGuidanceAvailability(
        status="retrieval_failed",
        reason="The cutoff-native guidance source could not be retrieved.",
    )
    revised = revise_historical_guidance_availability(legacy, availability)
    paths = write_historical_context_card_artifacts(revised, tmp_path)

    assert revised.content_sha256 == legacy.content_sha256
    assert revised.status == "reviewed"
    assert revised.management_guidance_availability == availability
    assert paths.artifact_json_path.read_bytes() == revised.canonical_bytes()
    with pytest.raises(ValueError, match="already explicit"):
        revise_historical_guidance_availability(revised, availability)
    with pytest.raises(ValidationError, match="requires a reason"):
        ManagementGuidanceAvailability(status="unavailable")
    with pytest.raises(ValidationError, match="cannot carry"):
        ManagementGuidanceAvailability(status="available", reason="not missing")
    with pytest.raises(ValidationError, match="must match the canonical ledger"):
        HistoricalContextCardArtifact.model_validate(
            {
                **legacy.model_dump(mode="python"),
                "management_guidance_availability": {"status": "available"},
            }
        )


def test_owner_approval_is_required_and_must_precede_target_cutoff() -> None:
    artifact = build_historical_context_card(_input())
    approved = approve_historical_context_card(
        artifact,
        approved_at=GENERATED_AT + timedelta(hours=1),
        approved_by="owner-analyst",
    )

    assert approved.status == "approved"
    assert approved.canonical_card.quality_control.requires_human_review is False
    assert approved.as_prior_evidence().receipt.source_ref == approved.citation_uri

    with pytest.raises(ValueError, match="precede the target cutoff"):
        approve_historical_context_card(
            artifact, approved_at=TARGET_CUTOFF, approved_by="owner-analyst"
        )


def test_readable_artifact_round_trips_through_existing_web_projection(
    tmp_path,
) -> None:
    artifact = build_historical_context_card(_input())
    paths = write_historical_context_card_artifacts(artifact, tmp_path)
    view = load_context_cards(paths.artifact_json_path)

    assert len(view.cards) == 1
    assert view.cards[0].executive_summary == artifact.executive_summary
    assert view.cards[0].reaction_metrics.t_plus_1_close_return_pct is not None
    assert "Executive summary" in paths.readable_markdown_path.read_text(
        encoding="utf-8"
    )


def test_generation_at_or_after_target_cutoff_fails_closed() -> None:
    with pytest.raises(ValidationError, match="strictly before the target cutoff"):
        _input(generated_at=TARGET_CUTOFF)


def test_builder_binds_live_enrichment_without_inventing_numeric_sentiment(
    tmp_path,
) -> None:
    news = NativeNewsLabelSummary(
        provider_version="2026-11",
        query_article_count=5,
        issuer_named_article_count=3,
        excluded_not_issuer_named_count=2,
        sentiment_label_counts={
            "positive": 2,
            "neutral": 1,
            "negative": 0,
            "unknown": 0,
        },
        raw_response_sha256="d" * 64,
        retrieved_at=GENERATED_AT - timedelta(minutes=2),
    )
    eps = ReconstructedEpsContext(
        provider_version="2026-07",
        estimated_eps=Decimal("0.13"),
        reported_eps=Decimal("-0.19"),
        raw_response_sha256="e" * 64,
        retrieved_at=GENERATED_AT - timedelta(minutes=3),
    )
    artifact = build_historical_context_card(
        _input(
            reaction=_reaction(with_after_hours=True),
            market_after_hours_source_sha256="f" * 64,
            benchmark_after_hours_source_sha256="1" * 64,
            native_news_summary=news,
            reconstructed_eps_context=eps,
        )
    )
    paths = write_historical_context_card_artifacts(artifact, tmp_path)
    markdown = paths.readable_markdown_path.read_text(encoding="utf-8")
    receipts = {
        receipt.id for receipt in artifact.canonical_card.evidence_registry.receipts
    }

    assert artifact.reaction_metrics["after_hours_return_pct"] is not None
    assert artifact.native_news_summary == news
    assert artifact.reconstructed_eps_context == eps
    assert "massive-news:DEMO:2026-08-10:FY2026Q2" in receipts
    assert "reconstructed-eps:DEMO:2026-08-10:FY2026Q2" in receipts
    assert "No numeric sentiment score was inferred" in markdown
    assert "excluded from surprise computation" in markdown


def test_retrospective_news_binding_is_visible_and_live_attachment_refuses_it(
    tmp_path,
) -> None:
    card = build_historical_context_card(_input())
    binding = HistoricalNewsEvidenceBinding(
        private_manifest_id=f"{card.event_id}:historical-private-news-v1",
        private_manifest_sha256="2" * 64,
        public_projection_sha256="3" * 64,
        source_retrieved_at=GENERATED_AT - timedelta(minutes=2),
        reconstructed_at=TARGET_CUTOFF + timedelta(days=1),
        item_count=4,
        content_available_count=0,
        headline_only_count=4,
        selected_passage_count=0,
        semantic_status="abstained_no_eligible_passages",
    )
    enriched = bind_historical_news_evidence(card, binding)
    approved = approve_historical_context_card(
        enriched,
        approved_at=GENERATED_AT + timedelta(hours=1),
        approved_by="owner-analyst",
    )
    markdown = write_historical_context_card_artifacts(
        enriched, tmp_path
    ).readable_markdown_path.read_text(encoding="utf-8")

    assert enriched.news_evidence_binding == binding
    assert "abstained_no_eligible_passages" in markdown
    assert any(
        item.id == f"historical-news-evidence:{card.event_id}"
        for item in enriched.canonical_card.evidence_registry.receipts
    )
    with pytest.raises(ValueError, match="cannot attach to a live bundle"):
        approved.as_prior_evidence()
    assert approved.as_replay_prior_evidence().card_id == card.card_id


def test_post_event_summaries_require_material_directional_divergence() -> None:
    unavailable = evaluate_post_event_explanation_gate(
        observed_benchmark_adjusted_t1=Decimal("-0.30")
    )
    small = evaluate_post_event_explanation_gate(
        expected_reaction_direction="positive",
        observed_benchmark_adjusted_t1=Decimal("-0.05"),
    )
    aligned = evaluate_post_event_explanation_gate(
        expected_reaction_direction="positive",
        observed_benchmark_adjusted_t1=Decimal("0.20"),
    )
    divergent = evaluate_post_event_explanation_gate(
        expected_reaction_direction="positive",
        observed_benchmark_adjusted_t1=Decimal("-0.20"),
    )

    assert unavailable.status == "unavailable_no_comparison_baseline"
    assert small.status == "not_triggered_small_reaction"
    assert aligned.status == "not_triggered_aligned"
    assert divergent.status == "triggered_divergence"

    post_ref = ProviderSummaryReference(
        article_id="massive-news-post-1",
        article_metadata_sha256="4" * 64,
        summary_sha256="5" * 64,
        published_at=EVENT_AT + timedelta(hours=1),
        phase=ProviderSummaryPhase.POST_EVENT,
        provider_native_sentiment_label="negative",
        selection_score=7,
        selection_reasons=(ProviderSummarySelectionReason.ISSUER_SPECIFIC,),
    )
    common = {
        "private_manifest_id": "historical-private-v1",
        "private_manifest_sha256": "2" * 64,
        "public_projection_sha256": "3" * 64,
        "source_retrieved_at": GENERATED_AT - timedelta(minutes=2),
        "reconstructed_at": GENERATED_AT,
        "item_count": 1,
        "content_available_count": 0,
        "summary_only_count": 1,
        "headline_only_count": 0,
        "selected_passage_count": 0,
        "selected_provider_summaries": (post_ref,),
        "semantic_status": "summary_backed",
    }
    with pytest.raises(ValidationError, match="triggered divergence"):
        HistoricalNewsEvidenceBinding(**common)
    accepted = HistoricalNewsEvidenceBinding(
        **common, post_event_explanation_gate=divergent
    )
    assert accepted.post_event_explanation_gate.status == "triggered_divergence"


def test_private_summary_pulls_render_in_historical_view_not_public_projection(
    tmp_path,
) -> None:
    direct_text = (
        "Article description: Example Systems expects revenue and demand to remain material.\n\n"
        "Ticker sentiment reasoning: The provider viewed the demand outlook positively."
    )
    article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="massive-news-summary-1",
        headline="Example Systems demand outlook before earnings",
        provider_summary=direct_text,
        summary_status=NewsSummaryStatus.RETAINED_PERMITTED,
        source_name="Example News",
        source_domain="news.example",
        url="https://example.com/news/demo-outlook",
        published_at=EVENT_AT - timedelta(days=1),
        retrieved_at=GENERATED_AT - timedelta(minutes=2),
        related_tickers=("DEMO",),
        ticker_match="exact",
        ticker_sentiment_label="positive",
        provider_fields={
            "summary_grant_id": "owner-private-research",
            "summary_grant_version": "v1",
        },
    )
    negative_text = (
        "Article description: Funding dependence and capex remain material.\n\n"
        "Ticker sentiment reasoning: Capital expenditure and external financing "
        "remain risks."
    )
    negative_article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="massive-news-summary-2",
        headline="Example Systems funding risk before earnings",
        provider_summary=negative_text,
        summary_status=NewsSummaryStatus.RETAINED_PERMITTED,
        source_name="Example News",
        source_domain="news.example",
        url="https://example.com/news/demo-funding",
        published_at=EVENT_AT - timedelta(hours=12),
        retrieved_at=GENERATED_AT - timedelta(minutes=2),
        related_tickers=("DEMO",),
        ticker_match="exact",
        ticker_sentiment_label="negative",
        provider_fields={
            "summary_grant_id": "owner-private-research",
            "summary_grant_version": "v1",
        },
    )
    manifest = build_news_evidence_manifest(
        manifest_id="historical-private-summary-v1",
        acceptance_manifest_sha256="d" * 64,
        ticker="DEMO",
        cutoff_at=TARGET_CUTOFF,
        collected_at=GENERATED_AT - timedelta(minutes=2),
        provider_response_sha256="e" * 64,
        articles=(article, negative_article),
        issuer_terms=("DEMO", "Example Systems"),
        maximum_direct_fetches=0,
    )
    references = select_provider_summary_references(
        manifest, event_at=EVENT_AT, issuer_terms=("DEMO", "Example Systems")
    )
    pulls = build_historical_provider_summary_pulls(manifest, references)
    stale = (
        "Massive news evidence is limited to rights-safe headline metadata and "
        "provider-native categorical labels; no numeric relevance or sentiment "
        "value was inferred."
    )
    base_input = _input()
    card = build_historical_context_card(
        _input(
            narrative=base_input.narrative.model_copy(
                update={"caveats": (stale, *base_input.narrative.caveats)}
            )
        )
    )
    bridge = build_historical_semantic_reaction_bridge(
        ticker=card.ticker,
        pulls=pulls,
        reaction=card.canonical_card.market_reaction_history[0],
    )
    binding = HistoricalNewsEvidenceBinding(
        private_manifest_id=manifest.manifest_id,
        private_manifest_sha256=manifest.manifest_sha256,
        public_projection_sha256=manifest.public_projection().projection_sha256,
        source_retrieved_at=GENERATED_AT - timedelta(minutes=2),
        reconstructed_at=TARGET_CUTOFF + timedelta(days=1),
        item_count=2,
        content_available_count=0,
        summary_only_count=2,
        headline_only_count=0,
        selected_passage_count=0,
        selected_provider_summaries=references,
        provider_summary_pulls=pulls,
        semantic_reaction_bridge=bridge,
        semantic_status="summary_backed",
    )
    enriched = bind_historical_news_evidence(card, binding)
    markdown = write_historical_context_card_artifacts(
        enriched, tmp_path
    ).readable_markdown_path.read_text(encoding="utf-8")

    assert "## Selected pre-event semantic evidence" in markdown
    assert "Example Systems expects revenue and demand to remain material." in markdown
    assert "The provider viewed the demand outlook positively." in markdown
    assert "Why it mattered entering earnings" in markdown
    assert "Question seed" in markdown
    assert "### Qualitative–quantitative bridge" in markdown
    assert "provider-summary text centered on capacity and demand" in markdown
    assert "positively labeled summaries emphasized" in markdown
    assert "negatively labeled summaries emphasized capital, funding" in markdown
    assert "1 positive, 0 neutral, 1 negative, 0 unknown" in markdown
    assert "directionally balanced incoming narrative" in markdown
    assert "benchmark-adjusted T+1 move" in markdown
    assert "Provider labels are not return forecasts" in markdown
    assert "## Reported event facts" in markdown
    assert "## Known limitations and intentionally omitted fields" in markdown
    assert "## Canonical v4 card" not in markdown
    assert "## 3. Executive decision context" not in markdown
    assert stale not in markdown
    assert "private summary resolver under a versioned grant" in markdown
    assert direct_text not in manifest.public_projection().canonical_bytes().decode()
    assert negative_text not in manifest.public_projection().canonical_bytes().decode()

    forged = pulls[0].model_dump(mode="python")
    forged["direct_text"] = "Changed summary text."
    with pytest.raises(ValidationError, match="does not match its reference"):
        type(pulls[0]).model_validate(forged)

    mismatched_bridge = bridge.model_copy(
        update={"benchmark_adjusted_t1": Decimal("-0.25")}
    )
    mismatched_binding = binding.model_copy(
        update={"semantic_reaction_bridge": mismatched_bridge}
    )
    with pytest.raises(ValueError, match="must match benchmark-adjusted returns"):
        bind_historical_news_evidence(card, mismatched_binding)
