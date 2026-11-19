"""Deterministic assembly of provider news metadata into evidence receipts."""

from __future__ import annotations

from datetime import date, datetime

from eventlens.providers.models import CompanyNewsMetadata, ProviderResponse

from .evidence import (
    EvidenceClass,
    NewsEvidencePhase,
    NewsEvidenceReceipt,
    NewsRightsStatus,
    NewsSignalBasis,
)


def build_news_receipts(
    response: ProviderResponse[CompanyNewsMetadata],
    *,
    query_start: date,
    query_end: date,
    phase: NewsEvidencePhase,
    cutoff_at: datetime,
    rights_status: NewsRightsStatus = NewsRightsStatus.PROVIDER_TERMS_REVIEW_PENDING,
) -> tuple[NewsEvidenceReceipt, ...]:
    """Bind normalized provider rows to their immutable response provenance.

    The provider adapter owns the canonical row hash; this assembler refuses
    caller-supplied or missing hashes and copies the response checksum and
    retrieval timestamp directly from the frozen provenance object.
    """
    receipts: list[NewsEvidenceReceipt] = []
    for article in response.data.articles:
        if not article.item_sha256:
            raise ValueError("news provider row is missing deterministic item_sha256")
        if article.item_sha256 != article.canonical_sha256():
            raise ValueError("news provider row hash does not match canonical metadata")
        relevance = article.ticker_relevance_score
        sentiment = article.ticker_sentiment_score
        receipts.append(
            NewsEvidenceReceipt(
                id=f"news:{response.provenance.provider_name}:{article.provider_article_id}",
                evidence_class=EvidenceClass.RECONSTRUCTED,
                claim=f"{response.provenance.provider_name} reported metadata for {article.ticker}",
                source_ref=f"{response.provenance.provider_name}:{article.provider_article_id}",
                provider_name=response.provenance.provider_name,
                provider_article_id=article.provider_article_id,
                ticker=article.ticker,
                query_start=query_start,
                query_end=query_end,
                cutoff_at=cutoff_at,
                evidence_phase=phase,
                item_sha=article.item_sha256,
                response_sha=response.provenance.raw_response_sha256,
                headline=article.headline,
                url=article.url,
                published_at=article.published_at,
                retrieved_at=response.provenance.retrieved_at,
                relevance_value=relevance,
                sentiment_value=sentiment,
                sentiment_label=article.ticker_sentiment_label,
                source_name=article.source_name,
                source_domain=article.source_domain,
                category=article.category,
                related_tickers=article.related_tickers,
                relevance_basis=(NewsSignalBasis.PROVIDER_NATIVE if relevance is not None else NewsSignalBasis.NOT_PROVIDED),
                sentiment_basis=(NewsSignalBasis.PROVIDER_NATIVE if sentiment is not None or article.ticker_sentiment_label else NewsSignalBasis.NOT_PROVIDED),
                rights_status=rights_status,
            )
        )
    return tuple(receipts)


__all__ = ["build_news_receipts"]
