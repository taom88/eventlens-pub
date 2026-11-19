from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from eventlens.agents.news import (
    NEWS_SKILL_DESCRIPTORS,
    NewsClassificationRequest,
    NewsClassificationResult,
    NewsClassifiedClaim,
    NewsEntityMatch,
    NewsEventRelationship,
    NewsHorizon,
    NewsImplication,
    NewsMateriality,
    NewsNovelty,
    NewsPassageCitation,
    NewsProviderNativeSentiment,
    NewsRelevance,
    NewsSummaryCitation,
    NewsTone,
    NewsTopic,
    NewsUsageReceipt,
    news_classification_json_schema,
)
from eventlens.providers import (
    ContentFetchPolicy,
    ContentHttpResponse,
    ContentRetentionPolicy,
    ContentSourceKind,
    NewsArticleMetadata,
    RightsAwareContentFetcher,
)
from eventlens.providers.passage_selection import (
    ArticleCandidate,
    article_candidate_from_content,
    cluster_duplicates,
    select_content_passages,
)


NOW = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 7, 2, 16, tzinfo=timezone.utc)
NEWS_FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "evals" / "news"


def _article(*, summary: bool = False, ticker: str = "DEMO", article_id: str = "news-1") -> NewsArticleMetadata:
    values: dict[str, object] = dict(
        ticker=ticker, provider_article_id=article_id, headline="Example Systems raises cloud outlook",
        url="https://issuer.example/release", published_at=datetime(2026, 7, 2, 10, tzinfo=timezone.utc),
        overall_sentiment_score=Decimal("0.1"), overall_sentiment_label="Neutral",
        ticker_relevance_score=Decimal("0.9"), ticker_sentiment_score=Decimal("0.3"),
        ticker_sentiment_label="Bullish",
    )
    if summary:
        values.update(provider_summary="Example Systems raised cloud outlook and revenue guidance.", summary_status="retained_permitted")
    return NewsArticleMetadata(**values)


def _policy(retention: ContentRetentionPolicy = ContentRetentionPolicy.FULL_BODY) -> ContentFetchPolicy:
    return ContentFetchPolicy(
        source_kind=ContentSourceKind.ISSUER, grant_id="issuer-news-v1", grant_version="1",
        source_ref="terms:issuer", allowed_hosts=("issuer.example",), access_permitted=True,
        robots_permitted=True, retention_policy=retention, max_excerpt_characters=800,
    )


def _response() -> ContentHttpResponse:
    return ContentHttpResponse(
        status_code=200, headers={"content-type": "text/html"}, retrieved_at=NOW,
        body=("<article><p>Example Systems raised revenue guidance to $10 billion as cloud demand accelerated. "
              "Management said cloud demand remains strong.</p></article>").encode(),
    )


def _request(
    retention: ContentRetentionPolicy = ContentRetentionPolicy.FULL_BODY, *, summary: bool = False,
    duplicate: bool = False, ticker: str = "DEMO", article_id: str = "news-1",
) -> NewsClassificationRequest:
    article = _article(summary=summary, ticker=ticker, article_id=article_id)
    policy = _policy(retention)
    evidence = RightsAwareContentFetcher(
        clock=lambda: NOW, resolver=lambda _: ("93.184.216.34",)
    ).fetch(
        article, cutoff_at=CUTOFF, policy=policy, request=lambda _: _response(),
    )
    selection = select_content_passages(
        evidence, article=article, fetch_policy=policy, issuer_terms=("Example Systems", "DEMO"), issuer_context=True,
    )
    clustering = None
    if duplicate:
        other = ArticleCandidate(
            article_id="wire-copy", headline=article.headline,
            text="Example Systems raised revenue guidance to $10 billion as cloud demand accelerated. Management said cloud demand remains strong.",
            discovery_fingerprint="f" * 64, published_at=article.published_at,
        )
        clustering = cluster_duplicates((
            article_candidate_from_content(article, evidence),
            other,
        ))
    return NewsClassificationRequest(
        request_id="news-request-1", ticker=ticker, cutoff_at=CUTOFF, article=article,
        content_evidence=evidence, passage_selection=selection,
        duplicate_clustering=clustering,
        allowed_ontology_slot_ids=("issuer.guidance",), prompt_version="news-prompt-v1",
    )


def _result(request: NewsClassificationRequest) -> NewsClassificationResult:
    selected = request.passage_selection.selected_passages
    summary_sha = request.provider_summary_sha256
    claims = () if not selected and summary_sha is None else (
        NewsClassifiedClaim(
            claim_id="guidance-1", text="Example Systems raised revenue guidance.", confidence=Decimal("0.9"),
            citations=(NewsPassageCitation(passage_id=selected[0].paragraph_id),) if selected else (),
            summary_citations=(NewsSummaryCitation(summary_sha256=summary_sha),) if not selected and summary_sha else (),
        ),
    )
    content_available = bool(selected) or summary_sha is not None
    return NewsClassificationResult(
        request=request, request_sha256=request.request_sha256 or "", entity_match=NewsEntityMatch.EXACT,
        event_relationship=NewsEventRelationship.PRE_EARNINGS,
        topics=(NewsTopic.GUIDANCE,) if content_available else (),
        ontology_slot_ids=("issuer.guidance",) if content_available else (),
        relevance=NewsRelevance.HIGH if content_available else NewsRelevance.UNKNOWN,
        materiality=NewsMateriality.HIGH if content_available else NewsMateriality.UNKNOWN,
        cited_claims=claims, agent_headline_tone=NewsTone.POSITIVE,
        agent_content_tone=NewsTone.POSITIVE if content_available else None,
        agent_fundamental_implication=NewsImplication.POSITIVE if content_available else None,
        agent_near_term_market_implication=NewsImplication.POSITIVE if content_available else None,
        horizon=NewsHorizon.NEXT_EARNINGS if content_available else None,
        novelty=NewsNovelty.DUPLICATE if request.duplicate_cluster else NewsNovelty.NOVEL, confidence=Decimal("0.8"),
        abstention_reason=None if content_available else "No retained selected passage is available.",
        provider_native_sentiment=NewsProviderNativeSentiment.from_article(request.article),
        prompt_version="news-prompt-v1",
        usage_receipt=NewsUsageReceipt(
            provider="fixture", model="fixture-news", prompt_version="news-prompt-v1",
            started_at=NOW, completed_at=NOW,
        ),
    )


def test_news_contract_binds_receipts_citations_and_checksum() -> None:
    request = _request()
    result = _result(request)

    assert request.request_sha256
    assert result.result_sha256 == result.content_checksum()
    assert result.cited_claims[0].citations[0].passage_id == request.passage_selection.selected_passages[0].paragraph_id
    assert result.provider_native_sentiment.ticker_sentiment_label == "Bullish"
    assert {item.skill_id for item in NEWS_SKILL_DESCRIPTORS} == {
        "content_claim_extractor", "news_materiality_and_sentiment"
    }


def test_claim_citation_and_provider_native_fields_cannot_be_forged() -> None:
    result = _result(_request())
    forged = result.model_dump(mode="json")
    forged["result_sha256"] = None
    forged["cited_claims"][0]["citations"][0]["passage_id"] = "unselected-passage"
    with pytest.raises(ValueError, match="selected passage"):
        NewsClassificationResult.model_validate(forged)

    forged = result.model_dump(mode="json")
    forged["result_sha256"] = None
    forged["provider_native_sentiment"]["ticker_sentiment_label"] = "Bearish"
    with pytest.raises(ValueError, match="copied exactly"):
        NewsClassificationResult.model_validate(forged)


def test_headline_only_forces_content_abstention() -> None:
    request = _request(ContentRetentionPolicy.METADATA_ONLY)
    assert not request.passage_selection.selected_passages
    result = _result(request)
    assert result.cited_claims == ()
    assert result.agent_content_tone is None
    assert result.abstention_reason

    forged = result.model_dump(mode="json")
    forged["result_sha256"] = None
    forged["agent_content_tone"] = "positive"
    with pytest.raises(ValueError, match="headline_only"):
        NewsClassificationResult.model_validate(forged)


def test_request_rejects_tampered_receipt_chain_and_result_request_checksum() -> None:
    request = _request()
    forged = request.model_dump(mode="json")
    forged["request_sha256"] = None
    forged["passage_selection"]["evidence_sha256"] = "0" * 64
    forged["passage_selection"]["selection_sha256"] = None
    with pytest.raises(ValueError, match="selection evidence checksum"):
        NewsClassificationRequest.model_validate(forged)

    result = _result(request)
    forged_result = result.model_dump(mode="json")
    forged_result["result_sha256"] = None
    forged_result["request_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="request_sha256"):
        NewsClassificationResult.model_validate(forged_result)


def test_summary_only_uses_checksum_bound_summary_citation_not_body_passage() -> None:
    request = _request(ContentRetentionPolicy.METADATA_ONLY, summary=True)
    assert request.content_evidence.status.value == "summary_only"
    assert all("provider_summary_reader" in item.allowed_read_only_tools for item in NEWS_SKILL_DESCRIPTORS)
    assert all("headline_or_summary_only" not in item.abstention_conditions for item in NEWS_SKILL_DESCRIPTORS)
    result = _result(request)
    assert result.cited_claims[0].summary_citations[0].summary_sha256 == request.provider_summary_sha256
    assert not result.cited_claims[0].citations

    forged = result.model_dump(mode="json")
    forged["result_sha256"] = None
    forged["cited_claims"][0]["summary_citations"][0]["summary_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="permitted provider summary"):
        NewsClassificationResult.model_validate(forged)


def test_exact_selected_passage_and_full_duplicate_clustering_are_bound() -> None:
    request = _request(duplicate=True)
    assert request.duplicate_clustering is not None
    assert request.duplicate_cluster is not None
    assert _result(request).novelty is NewsNovelty.DUPLICATE

    forged = request.model_dump(mode="json")
    forged["request_sha256"] = None
    selected = forged["passage_selection"]["selected_passages"][0]
    selected["excerpt"] = "Invented retained excerpt."
    selected["excerpt_sha256"] = hashlib.sha256(selected["excerpt"].encode()).hexdigest()
    forged["passage_selection"]["selection_sha256"] = None
    with pytest.raises(ValueError, match="exactly match retained content"):
        NewsClassificationRequest.model_validate(forged)

    forged_policy = request.model_dump(mode="json")
    forged_policy["request_sha256"] = None
    forged_policy["passage_selection"]["grant_id"] = "other-grant"
    forged_policy["passage_selection"]["selection_sha256"] = None
    with pytest.raises(ValueError, match="selection grant"):
        NewsClassificationRequest.model_validate(forged_policy)

    # This is a valid, rechecksummed complete clustering receipt, but its
    # purported input for this article is not the immutable news record.
    other = ArticleCandidate(
        article_id="wire-copy", headline=request.article.headline,
        text="Example Systems raised revenue guidance to $10 billion as cloud demand accelerated. Management said cloud demand remains strong.",
        discovery_fingerprint="e" * 64, published_at=request.article.published_at,
    )
    expected_candidate = article_candidate_from_content(request.article, request.content_evidence)
    forged_clustering = cluster_duplicates((
        expected_candidate.model_copy(update={"headline": "Forged headline"}),
        other,
    ))
    forged_cluster_request = request.model_dump(mode="json")
    forged_cluster_request["request_sha256"] = None
    forged_cluster_request["duplicate_clustering"] = forged_clustering.model_dump(mode="json")
    with pytest.raises(ValueError, match="exactly match content-derived"):
        NewsClassificationRequest.model_validate(forged_cluster_request)

    fabricated_clustering = cluster_duplicates((
        expected_candidate.model_copy(update={
            "text": "Fabricated duplicate text.",
            "source_url": None,
            "permitted_excerpt_sha256s": (),
        }),
        other,
    ))
    fabricated_request = request.model_dump(mode="json")
    fabricated_request["request_sha256"] = None
    fabricated_request["duplicate_clustering"] = fabricated_clustering.model_dump(mode="json")
    with pytest.raises(ValueError, match="exactly match content-derived"):
        NewsClassificationRequest.model_validate(fabricated_request)


@pytest.mark.parametrize(
    ("fixture_class", "headline_tone", "content_tone", "implication"),
    [
        ("positive", NewsTone.POSITIVE, NewsTone.POSITIVE, NewsImplication.POSITIVE),
        ("negative", NewsTone.NEGATIVE, NewsTone.NEGATIVE, NewsImplication.NEGATIVE),
        ("mixed", NewsTone.MIXED, NewsTone.MIXED, NewsImplication.MIXED),
    ],
)
def test_positive_negative_and_mixed_fixture_classes(fixture_class, headline_tone, content_tone, implication) -> None:
    payload = _result(_request()).model_dump(mode="json")
    payload.update({
        "agent_headline_tone": headline_tone,
        "agent_content_tone": content_tone,
        "agent_fundamental_implication": implication,
        "agent_near_term_market_implication": implication,
        "result_sha256": None,
    })
    result = NewsClassificationResult.model_validate(payload)
    assert result.agent_content_tone is content_tone, fixture_class


def test_irrelevant_fixture_class_is_coherent() -> None:
    request = _request()
    payload = _result(request).model_dump(mode="json")
    payload.update({
        "entity_match": NewsEntityMatch.MISMATCH,
        "relevance": NewsRelevance.NONE,
        "materiality": NewsMateriality.NONE,
        "cited_claims": (),
        "topics": (),
        "ontology_slot_ids": (),
        "agent_content_tone": None,
        "agent_fundamental_implication": None,
        "agent_near_term_market_implication": None,
        "horizon": None,
        "abstention_reason": "Issuer mismatch.",
        "result_sha256": None,
    })
    irrelevant = NewsClassificationResult.model_validate(payload)
    assert irrelevant.relevance is NewsRelevance.NONE


def test_abstained_fixture_class_and_schema_export() -> None:
    abstained = _result(_request(ContentRetentionPolicy.METADATA_ONLY))
    assert abstained.abstention_reason
    schema = news_classification_json_schema()
    assert schema["title"] == "NewsClassificationResult"


def test_headline_only_and_summary_only_cannot_bypass_cited_support() -> None:
    headline = _result(_request(ContentRetentionPolicy.METADATA_ONLY)).model_dump(mode="json")
    headline["result_sha256"] = None
    headline["materiality"] = NewsMateriality.HIGH
    with pytest.raises(ValueError, match="headline_only"):
        NewsClassificationResult.model_validate(headline)

    summary = _result(_request(ContentRetentionPolicy.METADATA_ONLY, summary=True)).model_dump(mode="json")
    summary["result_sha256"] = None
    summary["cited_claims"] = []
    with pytest.raises(ValueError, match="semantic judgments require cited claims"):
        NewsClassificationResult.model_validate(summary)


def test_checked_in_news_evaluation_fixtures_strictly_load_and_match_schema() -> None:
    fixture_paths = tuple(sorted(NEWS_FIXTURE_DIR.glob("*.json")))
    assert {path.stem for path in fixture_paths} == {
        "positive", "negative", "mixed", "irrelevant", "duplicate", "abstained",
    }
    schema = news_classification_json_schema()
    result_fields = set(NewsClassificationResult.model_fields)
    fixture_fields = result_fields - {"schema_version", "request", "request_sha256", "result_sha256"}
    modes = {
        "positive": "body", "negative": "body", "mixed": "body", "irrelevant": "body",
        "duplicate": "duplicate", "abstained": "headline_only",
    }
    for path in fixture_paths:
        raw = json.loads(path.read_text())
        assert set(raw) == {"fixture_class", "request_mode", "classification"}, path
        assert raw["fixture_class"] == path.stem
        assert raw["request_mode"] == modes[path.stem]
        assert set(raw["classification"]) == fixture_fields
        request = (
            _request(duplicate=True) if raw["request_mode"] == "duplicate"
            else _request(ContentRetentionPolicy.METADATA_ONLY) if raw["request_mode"] == "headline_only"
            else _request()
        )
        payload = {
            "schema_version": "eventlens.news-classification.v1",
            "request": request.model_dump(mode="json"),
            "request_sha256": request.request_sha256,
            "result_sha256": None,
            **raw["classification"],
        }
        # The checked-in fragment covers every semantic result field.  The
        # fixture loader supplies the immutable receipt envelope, then the
        # final document must satisfy both Pydantic and the exported schema.
        assert set(payload) == set(schema["properties"])
        loaded = NewsClassificationResult.model_validate_json(json.dumps(payload))
        assert loaded.model_dump(mode="json", exclude={
            "schema_version", "request", "request_sha256", "result_sha256",
        }) == raw["classification"]
        assert loaded.result_sha256 == loaded.content_checksum()
