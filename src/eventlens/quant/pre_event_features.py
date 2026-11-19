"""Deterministic metadata-only pre-event feature construction."""
from __future__ import annotations
from datetime import date, datetime, timezone
import hashlib
import json
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field
from eventlens.providers.models import CompanyNewsMetadata, EarningsEventRecord, MarketHistory, ProviderProvenance

class PreEventFeatureSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticker: str
    event_id: str
    event_date: date
    cutoff_at: datetime
    article_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    topic_counts: dict[str, int]
    mean_ticker_relevance: Decimal | None = None
    weighted_sentiment: Decimal | None = None
    recency_weighted_sentiment: Decimal | None = None
    sentiment_recency_half_life_days: Decimal = Decimal("7")
    sentiment_positive_share: Decimal | None = None
    latest_close: Decimal | None = None
    prior_5d_return_pct: Decimal | None = None
    prior_20d_return_pct: Decimal | None = None
    realized_outcome_status: str = "not_attached"
    recommendation_status: str = "not_generated"
    provenance_manifest: dict[str, object] = Field(default_factory=dict)

def build_pre_event_features(
    event: EarningsEventRecord,
    *,
    news: CompanyNewsMetadata,
    market: MarketHistory,
    cutoff_at: datetime,
    sentiment_recency_half_life_days: Decimal = Decimal("7"),
) -> PreEventFeatureSnapshot:
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must be timezone-aware")
    cutoff = cutoff_at.astimezone(timezone.utc)
    if sentiment_recency_half_life_days <= 0:
        raise ValueError("sentiment_recency_half_life_days must be positive")
    if cutoff.date() > event.event_date:
        raise ValueError("cutoff_at cannot be after event_date")
    if news.ticker != event.ticker or market.ticker != event.ticker:
        raise ValueError("event, news, and market tickers must match")
    if any(item.published_at > cutoff for item in news.articles):
        raise ValueError("news contains post-cutoff metadata")
    articles = tuple(item for item in news.articles if item.published_at <= cutoff)
    sources = {item.source_name or item.source_domain or "unknown" for item in articles}
    topics: dict[str, int] = {}
    for item in articles:
        if item.category:
            topics[item.category] = topics.get(item.category, 0) + 1
    relevant = tuple(item for item in articles if item.ticker_relevance_score is not None)
    sentiments = tuple(item for item in relevant if item.ticker_sentiment_score is not None)
    relevance = sum((item.ticker_relevance_score for item in relevant), Decimal("0")) / Decimal(len(relevant)) if relevant else None
    weighted = None
    recency_weighted = None
    positive_share = None
    if sentiments:
        weights = tuple(item.ticker_relevance_score or Decimal("0") for item in sentiments)
        denominator = sum(weights, Decimal("0"))
        weighted = sum((item.ticker_sentiment_score or Decimal("0")) * weight for item, weight in zip(sentiments, weights)) / denominator if denominator else None
        # Deterministic exponential decay: every seven days (the default), an
        # article receives half the weight of an otherwise identical article.
        # Provider-native relevance remains part of the weight, so recency
        # cannot make an irrelevant article influential.
        recency_weights = tuple(
            weight * (Decimal("2") ** (-_article_age_days(item.published_at, cutoff) / sentiment_recency_half_life_days))
            for item, weight in zip(sentiments, weights)
        )
        recency_denominator = sum(recency_weights, Decimal("0"))
        recency_weighted = (
            sum(
                (item.ticker_sentiment_score or Decimal("0")) * weight
                for item, weight in zip(sentiments, recency_weights)
            )
            / recency_denominator
            if recency_denominator
            else None
        )
        positive_share = Decimal(sum(1 for item in sentiments if (item.ticker_sentiment_score or Decimal("0")) > 0)) / Decimal(len(sentiments))
    bars = tuple(sorted((bar for bar in market.bars if bar.trading_date < event.event_date), key=lambda bar: bar.trading_date))
    latest = bars[-1] if bars else None
    return PreEventFeatureSnapshot(ticker=event.ticker, event_id=event.source_event_id, event_date=event.event_date, cutoff_at=cutoff, article_count=len(articles), source_count=len(sources), topic_counts=topics, mean_ticker_relevance=relevance, weighted_sentiment=weighted, recency_weighted_sentiment=recency_weighted, sentiment_recency_half_life_days=sentiment_recency_half_life_days, sentiment_positive_share=positive_share, latest_close=latest.close if latest else None, prior_5d_return_pct=_return(bars[-1].close, bars[-6].close) if len(bars) >= 6 else None, prior_20d_return_pct=_return(bars[-1].close, bars[-21].close) if len(bars) >= 21 else None)


def compact_provenance_manifest(*, news: ProviderProvenance, market: ProviderProvenance, cutoff_at: datetime, calculation_version: str = "pre-event-features-v1") -> dict[str, object]:
    """Return a small reproducibility manifest; raw provider bodies are excluded."""
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must be timezone-aware")
    document = {"provider_response_shas": [news.raw_response_sha256, market.raw_response_sha256], "provider_versions": [news.provider_version, market.provider_version], "cutoff_at": cutoff_at.astimezone(timezone.utc).isoformat(), "calculation_version": calculation_version}
    document["manifest_sha256"] = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return document

def _return(end: Decimal, start: Decimal) -> Decimal:
    return (end / start) - Decimal("1")


def _article_age_days(published_at: datetime, cutoff: datetime) -> Decimal:
    """Return non-negative UTC age in days for the recency decay function."""
    published = published_at.astimezone(timezone.utc)
    seconds = Decimal(str((cutoff - published).total_seconds()))
    return max(Decimal("0"), seconds / Decimal("86400"))

__all__ = ["PreEventFeatureSnapshot", "build_pre_event_features", "compact_provenance_manifest"]
