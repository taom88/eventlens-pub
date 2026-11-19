from __future__ import annotations

import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.test_news import CUTOFF, NOW, _policy, _request, _result  # noqa: E402

from eventlens.agents.news import (
    NewsClassificationRequest, NewsClassificationResult, NewsClassifiedClaim,
    NewsEntityMatch, NewsEventRelationship, NewsHorizon, NewsImplication,
    NewsMateriality, NewsNovelty, NewsPassageCitation, NewsProviderNativeSentiment,
    NewsRelevance, NewsTone, NewsTopic, NewsUsageReceipt,
)
from eventlens.artifacts.models import sha256_hex
from eventlens.product.news_signal import NewsCoverageQuality, NewsSignal, aggregate_news_signal
from eventlens.providers import (
    ContentHttpResponse, ContentRetentionPolicy, NewsArticleMetadata,
    RightsAwareContentFetcher,
)
from eventlens.providers.passage_selection import (
    DuplicateClustering, article_candidate_from_content, cluster_duplicates,
    select_content_passages,
)


def _variant_request(
    article_id: str,
    *,
    source: str = "issuer.example",
    published_at: datetime | None = None,
    retention: ContentRetentionPolicy = ContentRetentionPolicy.FULL_BODY,
    native_score: Decimal | None = Decimal("0.3"),
    clustering: DuplicateClustering | None = None,
) -> NewsClassificationRequest:
    """Build a complete, independently checksum-bound classification input."""

    article = NewsArticleMetadata(
        ticker="DEMO", provider_article_id=article_id,
        headline=f"Example Systems update {article_id}", url=f"https://issuer.example/{article_id}",
        source_domain=source, published_at=published_at or (CUTOFF - timedelta(hours=6)),
        overall_sentiment_score=native_score,
        overall_sentiment_label="Neutral" if native_score is not None else None,
        ticker_relevance_score=Decimal("0.9") if native_score is not None else None,
        ticker_sentiment_score=native_score,
        ticker_sentiment_label="Bullish" if native_score is not None else None,
    )
    policy = _policy(retention)
    evidence = RightsAwareContentFetcher(
        clock=lambda: NOW, resolver=lambda _: ("93.184.216.34",)
    ).fetch(
        article, cutoff_at=CUTOFF, policy=policy,
        request=lambda _: ContentHttpResponse(
            status_code=200, headers={"content-type": "text/html"}, retrieved_at=NOW,
            body=("<article><p>Example Systems raised revenue guidance to $10 billion as cloud demand "
                  "accelerated. Management said cloud demand remains strong.</p></article>").encode(),
        ),
    )
    selection = select_content_passages(
        evidence, article=article, fetch_policy=policy,
        issuer_terms=("Example Systems", "DEMO"), issuer_context=True,
    )
    return NewsClassificationRequest(
        request_id=f"request-{article_id}", ticker="DEMO", cutoff_at=CUTOFF,
        article=article, content_evidence=evidence, passage_selection=selection,
        duplicate_clustering=clustering, allowed_ontology_slot_ids=("issuer.guidance",),
        prompt_version="news-prompt-v1",
    )


def _variant_result(
    request: NewsClassificationRequest,
    *,
    content_tone: NewsTone = NewsTone.POSITIVE,
    headline_tone: NewsTone | None = None,
    topics: tuple[NewsTopic, ...] = (NewsTopic.GUIDANCE,),
    materiality: NewsMateriality = NewsMateriality.HIGH,
) -> NewsClassificationResult:
    selected = request.passage_selection.selected_passages
    content_available = bool(selected)
    return NewsClassificationResult(
        request=request, request_sha256=request.request_sha256 or "",
        entity_match=NewsEntityMatch.EXACT, event_relationship=NewsEventRelationship.PRE_EARNINGS,
        topics=tuple(sorted(topics, key=lambda item: item.value)) if content_available else (),
        ontology_slot_ids=("issuer.guidance",) if content_available else (),
        relevance=NewsRelevance.HIGH if content_available else NewsRelevance.UNKNOWN,
        materiality=materiality if content_available else NewsMateriality.UNKNOWN,
        cited_claims=(NewsClassifiedClaim(
            claim_id=f"claim-{request.article.provider_article_id}", text="Example Systems updated guidance.",
            confidence=Decimal("0.9"), citations=(NewsPassageCitation(
                passage_id=selected[0].paragraph_id,
            ),),
        ),) if content_available else (),
        agent_headline_tone=headline_tone or content_tone,
        agent_content_tone=content_tone if content_available else None,
        agent_fundamental_implication=NewsImplication.POSITIVE if content_available else None,
        agent_near_term_market_implication=NewsImplication.POSITIVE if content_available else None,
        horizon=NewsHorizon.NEXT_EARNINGS if content_available else None,
        novelty=NewsNovelty.DUPLICATE if request.duplicate_cluster else NewsNovelty.NOVEL,
        confidence=Decimal("0.8"),
        abstention_reason=None if content_available else "No retained selected passage is available.",
        provider_native_sentiment=NewsProviderNativeSentiment.from_article(request.article),
        prompt_version="news-prompt-v1",
        usage_receipt=NewsUsageReceipt(
            provider="fixture", model="fixture-news", prompt_version="news-prompt-v1",
            started_at=NOW, completed_at=NOW,
        ),
    )


def _rechecksum_signal(payload: dict[str, object]) -> None:
    payload["signal_sha256"] = sha256_hex({
        key: value for key, value in payload.items() if key != "signal_sha256"
    })


def test_positive_signal_is_decimal_checksumed_and_input_order_independent() -> None:
    result = _result(_request())
    first = aggregate_news_signal((result,), cutoff_at=CUTOFF)
    second = aggregate_news_signal(tuple(reversed((result,))), cutoff_at=CUTOFF)

    assert first == second
    assert first.weighted_content_score == Decimal("1")
    assert first.weighted_headline_score == Decimal("1")
    assert first.weighted_provider_native_score == Decimal("0.3")
    assert first.signal_sha256
    assert isinstance(first.items[0].weight, Decimal)
    assert NewsSignal.model_validate(first.model_dump(mode="json")) == first


def test_forged_rechecksummed_signal_and_post_cutoff_input_fail() -> None:
    result = _result(_request())
    signal = aggregate_news_signal((result,), cutoff_at=CUTOFF)
    forged = signal.model_dump(mode="json")
    forged["items"][0]["weight"] = "999"
    _rechecksum_signal(forged)
    with pytest.raises(ValueError, match="does not match deterministic"):
        NewsSignal.model_validate(forged)

    post_cutoff = result.model_copy(
        update={"request": result.request.model_copy(update={"article": result.request.article.model_copy(update={"published_at": CUTOFF})})}
    )
    with pytest.raises(ValueError, match="discovery_fingerprint|eligible before aggregation cutoff"):
        aggregate_news_signal((post_cutoff,), cutoff_at=CUTOFF)


def test_duplicate_cluster_counts_one_representative_and_preserves_full_receipt() -> None:
    first = _variant_request("duplicate-a", source="wire.example")
    second = _variant_request("duplicate-b", source="wire.example")
    clustering = cluster_duplicates((
        article_candidate_from_content(first.article, first.content_evidence),
        article_candidate_from_content(second.article, second.content_evidence),
    ))
    first = _variant_request("duplicate-a", source="wire.example", clustering=clustering)
    second = _variant_request("duplicate-b", source="wire.example", clustering=clustering)
    signal = aggregate_news_signal((_variant_result(second), _variant_result(first)), cutoff_at=CUTOFF)

    assert signal.duplicate_adjusted_breadth == 1
    assert signal.source_count == 1
    assert sum(item.included for item in signal.items) == 1
    assert {item.exclusion_reason for item in signal.items if not item.included} == {"duplicate_non_representative"}
    assert all(item.request.duplicate_clustering == clustering for item in signal.classifications)


def test_duplicate_member_cannot_mix_clustered_and_unclustered_receipts() -> None:
    first = _variant_request("mixed-duplicate-a")
    second = _variant_request("mixed-duplicate-b")
    clustering = cluster_duplicates((
        article_candidate_from_content(first.article, first.content_evidence),
        article_candidate_from_content(second.article, second.content_evidence),
    ))
    clustered_second = _variant_request("mixed-duplicate-b", clustering=clustering)

    with pytest.raises(ValueError, match="lacks the complete clustering receipt"):
        aggregate_news_signal((_variant_result(first), _variant_result(clustered_second)), cutoff_at=CUTOFF)


def test_same_topic_opposition_conflicts_but_unrelated_topics_do_not() -> None:
    positive = _variant_result(_variant_request("positive-guidance"), topics=(NewsTopic.GUIDANCE,))
    negative_guidance = _variant_result(
        _variant_request("negative-guidance", source="second.example"),
        content_tone=NewsTone.NEGATIVE, topics=(NewsTopic.GUIDANCE,),
    )
    negative_regulatory = _variant_result(
        _variant_request("negative-regulatory", source="third.example"),
        content_tone=NewsTone.NEGATIVE, topics=(NewsTopic.REGULATORY,),
    )
    negative_low = _variant_result(
        _variant_request("negative-low", source="fourth.example"),
        content_tone=NewsTone.NEGATIVE, topics=(NewsTopic.GUIDANCE,),
        materiality=NewsMateriality.LOW,
    )

    same_topic = aggregate_news_signal((positive, negative_guidance), cutoff_at=CUTOFF)
    unrelated = aggregate_news_signal((positive, negative_regulatory), cutoff_at=CUTOFF)
    low_materiality = aggregate_news_signal((positive, negative_low), cutoff_at=CUTOFF)
    assert same_topic.contradiction_score > Decimal("0")
    assert unrelated.contradiction_score == Decimal("0")
    assert low_materiality.contradiction_score == Decimal("0")


def test_breadth_source_diversity_and_missing_native_or_content_degrade() -> None:
    same_source = aggregate_news_signal((
        _variant_result(_variant_request("same-source-a", source="wire.example")),
        _variant_result(_variant_request("same-source-b", source="wire.example")),
    ), cutoff_at=CUTOFF)
    different_source = aggregate_news_signal((
        _variant_result(_variant_request("different-source-a", source="wire-a.example")),
        _variant_result(_variant_request("different-source-b", source="wire-b.example", native_score=None)),
    ), cutoff_at=CUTOFF)
    headline_only = aggregate_news_signal((
        _variant_result(_variant_request("headline-only", retention=ContentRetentionPolicy.METADATA_ONLY)),
    ), cutoff_at=CUTOFF)

    assert same_source.duplicate_adjusted_breadth == 2
    assert same_source.source_count == 1
    assert {item.source_diversity_factor for item in same_source.items} == {Decimal("0.5")}
    assert different_source.source_count == 2
    assert {item.source_diversity_factor for item in different_source.items} == {Decimal("1")}
    assert different_source.weighted_provider_native_score == Decimal("0.3")
    assert headline_only.coverage_quality is NewsCoverageQuality.NO_CONTENT
    assert headline_only.weighted_content_score is None
    assert headline_only.abstention_rate == Decimal("1")


def test_rechecksummed_tone_and_policy_factor_forgery_are_rederived() -> None:
    signal = aggregate_news_signal((_variant_result(_variant_request("forgery")),), cutoff_at=CUTOFF)

    forged_tone = signal.model_dump(mode="json")
    classification = dict(forged_tone["classifications"][0])
    classification["agent_content_tone"] = "negative"
    classification["result_sha256"] = None
    forged_tone["classifications"] = [
        NewsClassificationResult.model_validate(classification).model_dump(mode="json")
    ]
    _rechecksum_signal(forged_tone)
    with pytest.raises(ValueError, match="does not match deterministic inputs"):
        NewsSignal.model_validate(forged_tone)

    forged_factor = signal.model_dump(mode="json")
    forged_factor["items"][0]["confidence_factor"] = "0.1"
    forged_factor["items"][0]["weight"] = "0.1"
    _rechecksum_signal(forged_factor)
    with pytest.raises(ValueError, match="does not match deterministic inputs"):
        NewsSignal.model_validate(forged_factor)
