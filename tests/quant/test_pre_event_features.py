from datetime import date, datetime, timezone
from decimal import Decimal

from eventlens.domain.events import EventTiming
from eventlens.providers.models import CompanyNewsMetadata, DailyBar, EarningsEventRecord, MarketHistory, NewsArticleMetadata
from eventlens.quant.pre_event_features import build_pre_event_features


def test_pre_event_features_are_cutoff_bounded_and_metadata_only() -> None:
    event = EarningsEventRecord(ticker="BETA", event_date=date(2026, 1, 10), timing=EventTiming.AFTER_MARKET_CLOSE, source_event_id="BETA:2026-01-10:2025Q4")
    news = CompanyNewsMetadata(ticker="BETA", articles=(NewsArticleMetadata(
        ticker="BETA", provider_article_id="n1", headline="Memory demand", source_name="Example", url="https://example.com/n1",
        published_at=datetime(2026, 1, 9, 15, tzinfo=timezone.utc), category="Demand",
        ticker_relevance_score=Decimal("0.8"), ticker_sentiment_score=Decimal("0.5"),
    ),))
    bars = tuple(DailyBar(trading_date=date(2026, 1, day), open=Decimal("100"), high=Decimal("110"), low=Decimal("99"), close=Decimal(str(100 + day)), volume=100) for day in range(1, 10))
    snapshot = build_pre_event_features(event, news=news, market=MarketHistory(ticker="BETA", bars=bars), cutoff_at=datetime(2026, 1, 10, 19, 59, tzinfo=timezone.utc))
    assert snapshot.article_count == 1
    assert snapshot.source_count == 1
    assert snapshot.topic_counts == {"Demand": 1}
    assert snapshot.weighted_sentiment == Decimal("0.5")
    assert snapshot.recency_weighted_sentiment == Decimal("0.5")
    assert snapshot.sentiment_recency_half_life_days == Decimal("7")
    assert snapshot.recommendation_status == "not_generated"


def test_recency_weighting_overweights_recent_news_with_seven_day_half_life() -> None:
    event = EarningsEventRecord(
        ticker="BETA", event_date=date(2026, 1, 10), timing=EventTiming.AFTER_MARKET_CLOSE,
        source_event_id="BETA:2026-01-10:2025Q4",
    )
    cutoff = datetime(2026, 1, 10, 19, 59, tzinfo=timezone.utc)
    news = CompanyNewsMetadata(ticker="BETA", articles=(
        NewsArticleMetadata(
            ticker="BETA", provider_article_id="old", headline="Older demand", source_name="Example",
            url="https://example.com/old", published_at=datetime(2026, 1, 3, 19, 59, tzinfo=timezone.utc),
            category="Demand", ticker_relevance_score=Decimal("1"), ticker_sentiment_score=Decimal("-1"),
        ),
        NewsArticleMetadata(
            ticker="BETA", provider_article_id="recent", headline="Recent demand", source_name="Example",
            url="https://example.com/recent", published_at=cutoff, category="Demand",
            ticker_relevance_score=Decimal("1"), ticker_sentiment_score=Decimal("1"),
        ),
    ))
    snapshot = build_pre_event_features(
        event, news=news, market=MarketHistory(ticker="BETA", bars=()), cutoff_at=cutoff,
    )
    assert snapshot.weighted_sentiment == Decimal("0")
    assert snapshot.recency_weighted_sentiment > Decimal("0")
