from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from eventlens.config import EventLensSettings
from eventlens.domain.events import EventTiming
from eventlens.providers import (
    AlphaVantageProvider,
    FinnhubProvider,
    MassiveProvider,
    MassiveSummaryRetentionGrant,
    NewsSummaryStatus,
    NewsTickerMatch,
    SecEdgarProvider,
)
from eventlens.providers.errors import (
    ProviderConfigurationError,
    ProviderEntitlementError,
    ProviderPayloadError,
)
from eventlens.providers.fixture import FixtureProvider
from eventlens.providers.http import (
    CachedHttpClient,
    ProviderHTTPError,
    ProviderQuotaExceededError,
)
from eventlens.providers.registry import build_free_first_registry
from eventlens.providers.yfinance_provider import YFinanceProvider


def _client(tmp_path: Path, handler) -> CachedHttpClient:
    transport = httpx.MockTransport(handler)
    return CachedHttpClient(
        tmp_path,
        client=httpx.Client(transport=transport),
        clock=lambda: datetime(2026, 7, 18, 12, tzinfo=UTC),
    )


def test_alpha_vantage_normalizes_and_caches_transcript_without_leaking_key(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["apikey"] == "alpha-secret"
        return httpx.Response(
            200,
            json={
                "symbol": "DEMO",
                "quarter": "2026Q2",
                "date": "2026-04-29",
                "transcript": [
                    {
                        "speaker": "Satya Nadella",
                        "title": "CEO",
                        "section": "prepared remarks",
                        "company": "Example Systems",
                        "firm": "Example Systems Investor Relations",
                        "content": "A source-grounded passage.",
                    }
                ],
            },
        )

    http = _client(tmp_path, handler)
    provider = AlphaVantageProvider("alpha-secret", http, daily_budget=20)

    first = provider.get_transcript("demo", "2026q2")
    second = provider.get_transcript("DEMO", "2026Q2")

    assert calls == 1
    assert http.ledger.usage("alpha_vantage") == 1
    assert first.data.passages[0].passage_id == second.data.passages[0].passage_id
    assert first.data.passages[0].section == "prepared remarks"
    assert first.data.passages[0].company == "Example Systems"
    assert first.data.passages[0].firm == "Example Systems Investor Relations"
    assert first.provenance.quota_consumed is True
    assert second.provenance.quota_consumed is False
    assert "alpha-secret" not in json.dumps(second.model_dump(mode="json"))
    assert "alpha-secret" not in "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json")
    )


def test_alpha_vantage_daily_compact_response_is_normalized(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["function"] == "TIME_SERIES_DAILY"
        return httpx.Response(
            200,
            json={
                "Time Series (Daily)": {
                    "2026-07-02": {
                        "1. open": "501.00",
                        "2. high": "505.00",
                        "3. low": "499.00",
                        "4. close": "504.00",
                        "5. volume": "200",
                    },
                    "2026-07-01": {
                        "1. open": "499.00",
                        "2. high": "502.00",
                        "3. low": "498.00",
                        "4. close": "500.00",
                        "5. volume": "100",
                    },
                }
            },
        )

    provider = AlphaVantageProvider("key", _client(tmp_path, handler))
    result = provider.get_daily_history("DEMO", date(2026, 7, 1), date(2026, 7, 2))

    assert [bar.trading_date for bar in result.data.bars] == [
        date(2026, 7, 1),
        date(2026, 7, 2),
    ]
    assert result.data.bars[-1].close.as_tuple().digits == (5, 0, 4, 0, 0)
    assert result.provenance.data_quality.value == "degraded"
    assert result.provenance.warnings[0].code == "unadjusted_daily_prices"


def test_alpha_vantage_news_sentiment_is_metadata_only(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["function"] == "NEWS_SENTIMENT"
        assert request.url.params["tickers"] == "DEMO"
        assert request.url.params["time_from"] == "20260701T0000"
        assert request.url.params["time_to"] == "20260702T2359"
        return httpx.Response(
            200,
            json={
                "feed": [
                    {
                        "title": "Example Systems reports cloud results",
                        "url": "https://example.com/demo-cloud",
                        "time_published": "20260701T213000",
                        "authors": ["Reporter Name"],
                        "summary": "This closed article summary must not be retained.",
                        "banner_image": "https://example.com/image.jpg",
                        "source": "Example News",
                        "category_within_source": "Earnings",
                        "source_domain": "example.com",
                        "overall_sentiment_score": "0.21",
                        "overall_sentiment_label": "Somewhat-Bullish",
                        "ticker_sentiment": [
                            {
                                "ticker": "DEMO",
                                "relevance_score": "0.99",
                                "ticker_sentiment_score": "0.33",
                                "ticker_sentiment_label": "Bullish",
                            },
                            {
                                "ticker": "QQQ",
                                "relevance_score": "0.12",
                                "ticker_sentiment_score": "0.01",
                                "ticker_sentiment_label": "Neutral",
                            },
                        ],
                    }
                ]
            },
        )

    provider = AlphaVantageProvider("key", _client(tmp_path, handler))
    result = provider.get_company_news("demo", date(2026, 7, 1), date(2026, 7, 2))
    serialized = json.dumps(result.model_dump(mode="json"))

    assert len(result.data.articles) == 1
    article = result.data.articles[0]
    assert article.provider_article_id.startswith("av-news-")
    assert article.headline == "Example Systems reports cloud results"
    assert article.source_domain == "example.com"
    assert article.related_tickers == ("DEMO", "QQQ")
    assert str(article.ticker_sentiment_score) == "0.33"
    assert result.provenance.warnings[0].code == "metadata_only_news"
    assert "closed article summary" not in serialized
    assert "banner_image" not in serialized


def test_massive_news_is_cutoff_bounded_metadata_with_native_label(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer massive-secret"
        assert request.url.params["ticker"] == "DEMO"
        assert request.url.params["published_utc.gte"] == "2026-07-01T00:00:00Z"
        assert request.url.params["published_utc.lt"] == "2026-07-02T16:00:00Z"
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "id": "article-1",
                        "title": "Example Systems raises its outlook",
                        "description": "Provider text must remain outside normalized metadata.",
                        "article_url": "https://news.example.com/demo-outlook",
                        "published_utc": "2026-07-02T15:00:00Z",
                        "publisher": {
                            "name": "Example News",
                            "homepage_url": "https://www.news.example.com/",
                        },
                        "tickers": ["DEMO", "QQQ"],
                        "insights": [
                            {
                                "ticker": "DEMO",
                                "sentiment": "positive",
                                "sentiment_reasoning": "Reasoning is not retained.",
                            }
                        ],
                    },
                    {
                        "id": "article-at-cutoff",
                        "title": "At cutoff",
                        "article_url": "https://news.example.com/at-cutoff",
                        "published_utc": "2026-07-02T16:00:00Z",
                        "tickers": ["DEMO"],
                    },
                    {
                        "id": "article-naive-time",
                        "title": "Missing timezone",
                        "article_url": "https://news.example.com/naive",
                        "published_utc": "2026-07-02T15:30:00",
                        "tickers": ["DEMO"],
                    },
                ],
            },
        )

    http = _client(tmp_path, handler)
    provider = MassiveProvider("massive-secret", http)
    cutoff = datetime(2026, 7, 2, 16, tzinfo=UTC)
    first = provider.get_company_news(
        "demo", date(2026, 7, 1), date(2026, 7, 2), cutoff_at=cutoff
    )
    second = provider.get_company_news(
        "DEMO", date(2026, 7, 1), date(2026, 7, 2), cutoff_at=cutoff
    )

    assert calls == 1
    assert http.ledger.usage("massive") == 1
    assert second.provenance.origin.value == "cache"
    assert second.provenance.quota_consumed is False
    assert len(first.data.articles) == 1
    article = first.data.articles[0]
    assert article.provider_article_id == "massive-news-article-1"
    assert article.source_domain == "news.example.com"
    assert article.related_tickers == ("DEMO", "QQQ")
    assert article.ticker_match is NewsTickerMatch.RELATED
    assert article.ticker_sentiment_label == "positive"
    assert article.ticker_sentiment_score is None
    assert {warning.code for warning in first.provenance.warnings} >= {
        "metadata_only_news",
        "malformed_news_rows",
    }
    serialized = json.dumps(second.model_dump(mode="json"))
    assert "Provider text" not in serialized
    assert "Reasoning is not retained" not in serialized
    assert "massive-secret" not in serialized
    assert "massive-secret" not in "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json")
    )


def test_massive_news_retains_summary_and_reasoning_only_under_explicit_grant(
    tmp_path: Path,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "id": "summary-1",
                        "title": "Example Systems expands synthetic capacity",
                        "description": "Example Systems reported a synthetic capacity expansion.",
                        "article_url": "https://news.example.com/demo-capacity",
                        "published_utc": "2026-07-02T15:00:00Z",
                        "publisher": {"name": "Example News"},
                        "tickers": ["DEMO"],
                        "insights": [
                            {
                                "ticker": "DEMO",
                                "sentiment": "positive",
                                "sentiment_reasoning": (
                                    "The expansion could support future demand."
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    provider = MassiveProvider("massive-secret", _client(tmp_path, handler))
    grant = MassiveSummaryRetentionGrant(
        grant_id="owner-private-research", grant_version="v1"
    )
    result = provider.get_company_news(
        "DEMO",
        date(2026, 7, 1),
        date(2026, 7, 2),
        cutoff_at=datetime(2026, 7, 2, 16, tzinfo=UTC),
        summary_retention_grant=grant,
    )

    article = result.data.articles[0]
    assert article.summary_status is NewsSummaryStatus.RETAINED_PERMITTED
    assert article.provider_summary == (
        "Article description: Example Systems reported a synthetic capacity expansion.\n\n"
        "Ticker sentiment reasoning: The expansion could support future demand."
    )
    assert article.provider_fields == {
        "summary_grant_id": "owner-private-research",
        "summary_grant_version": "v1",
        "summary_authorized_use": "private_internal_research",
    }
    assert result.provenance.warnings[0].code == (
        "private_provider_summaries_retained"
    )


def test_massive_news_fails_closed_on_unbounded_pagination(tmp_path: Path) -> None:
    provider = MassiveProvider(
        "key",
        _client(
            tmp_path,
            lambda request: httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [],
                    "next_url": "https://api.massive.com/v2/reference/news?cursor=opaque",
                },
            ),
        ),
    )

    with pytest.raises(ProviderPayloadError, match="split the query"):
        provider.get_company_news("DEMO", date(2026, 7, 1), date(2026, 7, 2))


def test_massive_cache_only_news_miss_never_calls_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = MassiveProvider("key", _client(tmp_path, handler))
    with pytest.raises(ProviderHTTPError, match="cache-only request missed"):
        provider.get_company_news(
            "DEMO",
            date(2026, 7, 1),
            date(2026, 7, 2),
            cache_only=True,
        )

    assert calls == 0


def test_massive_daily_summary_retains_unadjusted_after_hours_close(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer massive-secret"
        assert request.url.path == "/v1/open-close/DEMO/2026-07-02"
        assert request.url.params["adjusted"] == "false"
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "symbol": "DEMO",
                "from": "2026-07-02",
                "open": 100.0,
                "high": 106.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 1234567,
                "preMarket": 99.5,
                "afterHours": 108.25,
            },
        )

    http = _client(tmp_path, handler)
    provider = MassiveProvider("massive-secret", http)
    first = provider.get_daily_ticker_summary("demo", date(2026, 7, 2))
    second = provider.get_daily_ticker_summary("DEMO", date(2026, 7, 2))

    assert calls == 1
    assert second.provenance.origin.value == "cache"
    assert first.data.adjusted is False
    assert first.data.close == Decimal("104.0")
    assert first.data.after_hours == Decimal("108.25")
    assert first.provenance.coverage_start == date(2026, 7, 2)
    serialized = json.dumps(second.model_dump(mode="json"))
    assert "massive-secret" not in serialized


def test_massive_daily_summary_rejects_adjusted_prices_before_http(
    tmp_path: Path,
) -> None:
    provider = MassiveProvider(
        "key", _client(tmp_path, lambda request: httpx.Response(500))
    )

    with pytest.raises(ValueError, match="adjusted=False"):
        provider.get_daily_ticker_summary("DEMO", date(2026, 7, 2), adjusted=True)


def test_alpha_vantage_news_rejects_cutoff_after_query_end_before_http(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = AlphaVantageProvider("key", _client(tmp_path, handler))

    with pytest.raises(ValueError, match="cutoff_at must not be after query_end"):
        provider.get_company_news(
            "DEMO",
            date(2026, 7, 1),
            date(2026, 7, 2),
            cutoff_at=datetime(2026, 7, 3, tzinfo=UTC),
        )

    assert calls == 0


def test_alpha_vantage_news_requires_utc_cutoff_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = AlphaVantageProvider("key", _client(tmp_path, handler))

    with pytest.raises(ValueError, match="cutoff_at must use UTC"):
        provider.get_company_news(
            "DEMO",
            date(2026, 7, 1),
            date(2026, 7, 2),
            cutoff_at=datetime(2026, 7, 2, 16, tzinfo=UTC).astimezone(
                timezone(timedelta(hours=-4))
            ),
        )

    assert calls == 0


def test_alpha_vantage_news_excludes_article_exactly_at_cutoff(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "feed": [
                    {
                        "title": "Before",
                        "url": "https://example.com/before",
                        "time_published": "20260702T155959",
                        "source": "Wire",
                        "ticker_sentiment": [],
                    },
                    {
                        "title": "At cutoff",
                        "url": "https://example.com/at",
                        "time_published": "20260702T160000",
                        "source": "Wire",
                        "ticker_sentiment": [],
                    },
                ]
            },
        )

    result = AlphaVantageProvider("key", _client(tmp_path, handler)).get_company_news(
        "DEMO",
        date(2026, 7, 1),
        date(2026, 7, 2),
        cutoff_at=datetime(2026, 7, 2, 16, tzinfo=UTC),
    )
    assert [article.headline for article in result.data.articles] == ["Before"]


def test_alpha_vantage_preserves_report_time_as_event_timing(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "quarterlyEarnings": [
                    {
                        "reportedDate": "2026-04-29",
                        "fiscalDateEnding": "2026-03-31",
                        "reportTime": "post-market",
                        "reportedEPS": "6.43",
                        "estimatedEPS": "5.28",
                    },
                    {
                        "reportedDate": "2026-01-28",
                        "fiscalDateEnding": "2025-12-31",
                        "reportTime": "pre-market",
                        "reportedEPS": "8.88",
                        "estimatedEPS": "8.10",
                    },
                ]
            },
        )

    result = AlphaVantageProvider(
        "key", _client(tmp_path, handler)
    ).get_earnings_events("DEMO", date(2026, 1, 1), date(2026, 5, 1))

    assert [item.timing for item in result.data.events] == [
        EventTiming.BEFORE_MARKET_OPEN,
        EventTiming.AFTER_MARKET_CLOSE,
    ]
    assert not result.provenance.warnings


def test_alpha_vantage_blocks_paid_options_before_spending_quota(
    tmp_path: Path,
) -> None:
    provider = AlphaVantageProvider(
        "key",
        _client(tmp_path, lambda request: httpx.Response(500)),
    )

    with pytest.raises(ProviderEntitlementError, match="premium"):
        provider.get_historical_options("DEMO", date(2026, 7, 1))

    assert provider._http.ledger.usage("alpha_vantage") == 0


def test_internal_alpha_budget_fails_closed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"quarterlyEarnings": []})

    http = _client(tmp_path, handler)
    provider = AlphaVantageProvider("key", http, daily_budget=1)
    provider.get_earnings_events("DEMO", date(2026, 1, 1), date(2026, 7, 1))

    with pytest.raises(ProviderQuotaExceededError, match="budget exhausted"):
        provider.get_earnings_events("AAPL", date(2026, 1, 1), date(2026, 7, 1))


def test_alpha_rate_limit_payload_is_never_cached(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"Note": "rate limit"})

    provider = AlphaVantageProvider("key", _client(tmp_path, handler), daily_budget=2)

    with pytest.raises(ProviderPayloadError, match="request unavailable"):
        provider.get_transcript("DEMO", "2026Q2")
    with pytest.raises(ProviderPayloadError, match="request unavailable"):
        provider.get_transcript("DEMO", "2026Q2")

    assert calls == 2
    assert not list((tmp_path / "alpha_vantage").glob("*.json"))


def test_cache_payload_and_provenance_are_derived_from_same_raw_bytes(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"quarterlyEarnings": []})

    http = _client(tmp_path, handler)
    provider = AlphaVantageProvider("key", http)
    first = provider.get_earnings_events("DEMO", date(2026, 1, 1), date(2026, 7, 1))
    cache_path = next((tmp_path / "alpha_vantage").glob("*.json"))
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["raw_json"] = '{"quarterlyEarnings":[{"reportedDate":"2026-04-01"}]}'
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    repeated = provider.get_earnings_events("DEMO", date(2026, 1, 1), date(2026, 7, 1))

    assert calls == 1
    assert repeated.data.events == ()
    assert (
        repeated.provenance.raw_response_sha256 == first.provenance.raw_response_sha256
    )


def test_finnhub_calendar_preserves_announcement_timing(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["token"] == "finn-secret"
        return httpx.Response(
            200,
            json={
                "earningsCalendar": [
                    {
                        "date": "2026-07-22",
                        "epsActual": None,
                        "epsEstimate": 3.38,
                        "hour": "amc",
                        "quarter": 4,
                        "symbol": "DEMO",
                        "year": 2026,
                    }
                ]
            },
        )

    provider = FinnhubProvider(
        "finn-secret", _client(tmp_path, handler), today=lambda: date(2026, 7, 18)
    )
    result = provider.get_earnings_events("DEMO", date(2026, 7, 1), date(2026, 7, 31))

    assert result.data.events[0].timing is EventTiming.AFTER_MARKET_CLOSE
    assert result.data.events[0].fiscal_quarter == "2026Q4"
    assert "token" not in result.provenance.request_parameters


def test_finnhub_company_news_normalizes_discovery_and_is_key_safe(
    tmp_path: Path,
) -> None:
    published = int(datetime(2026, 7, 1, 21, 30, tzinfo=UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/company-news")
        assert request.url.params["token"] == "finn-secret"
        return httpx.Response(
            200,
            json=[
                {
                    "category": "company",
                    "datetime": published,
                    "headline": "Example Systems announces quarterly results",
                    "id": 12345,
                    "image": "https://example.com/image.jpg",
                    "related": "DEMO, DEMO",
                    "source": "Example Wire",
                    "summary": "Provider supplied discovery summary.",
                    "url": "https://example.com/demo-results",
                }
            ],
        )

    provider = FinnhubProvider(
        "finn-secret",
        _client(tmp_path, handler),
        allow_provider_summary_retention=True,
    )
    result = provider.get_company_news("DEMO", date(2026, 7, 1), date(2026, 7, 2))
    serialized = json.dumps(result.model_dump(mode="json"))

    assert result.data.articles[0].provider_article_id == "finnhub-news-12345"
    assert (
        result.data.articles[0].headline
        == "Example Systems announces quarterly results"
    )
    article = result.data.articles[0]
    assert article.related_tickers == ("DEMO",)
    assert article.ticker_match is NewsTickerMatch.EXACT
    assert article.provider_summary == "Provider supplied discovery summary."
    assert article.retrieved_at == datetime(2026, 7, 18, 12, tzinfo=UTC)
    assert article.provider_fields == {"category": "company", "related": "DEMO, DEMO"}
    assert article.discovery_fingerprint is not None
    assert result.provenance.warnings[0].code == "discovery_summary_not_body"
    assert "https://example.com/image.jpg" not in serialized
    assert "finn-secret" not in serialized


def test_finnhub_summary_is_withheld_without_explicit_permission(
    tmp_path: Path,
) -> None:
    published = int(datetime(2026, 7, 1, 21, 30, tzinfo=UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "category": "company",
                    "datetime": published,
                    "headline": "Headline",
                    "id": 8,
                    "related": "DEMO",
                    "source": "Wire",
                    "summary": "Not licensed for retention.",
                    "url": "https://example.com/story",
                }
            ],
        )

    result = FinnhubProvider("key", _client(tmp_path, handler)).get_company_news(
        "DEMO",
        date(2026, 7, 1),
        date(2026, 7, 2),
    )
    article = result.data.articles[0]
    assert article.provider_summary is None
    assert article.summary_status.value == "withheld_no_permission"
    assert "Not licensed" not in json.dumps(result.model_dump(mode="json"))


def test_finnhub_news_excludes_malformed_future_and_duplicate_rows(
    tmp_path: Path,
) -> None:
    before = int(datetime(2026, 7, 2, 15, tzinfo=UTC).timestamp())
    cutoff = datetime(2026, 7, 2, 16, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        valid = {
            "category": "company",
            "datetime": before,
            "headline": "Valid",
            "id": 1,
            "related": "DEMO",
            "source": "Wire",
            "url": "https://example.com/valid",
        }
        return httpx.Response(
            200,
            json=[
                valid,
                valid,
                {"datetime": before, "id": 2, "url": "https://example.com/no-headline"},
                {**valid, "id": 3, "datetime": int(cutoff.timestamp())},
            ],
        )

    result = FinnhubProvider("key", _client(tmp_path, handler)).get_company_news(
        "DEMO",
        date(2026, 7, 1),
        date(2026, 7, 2),
        cutoff_at=cutoff,
    )
    assert len(result.data.articles) == 1
    article = result.data.articles[0]
    assert article.provider_summary is None
    assert article.ticker_match is NewsTickerMatch.EXACT
    warning_codes = {item.code for item in result.provenance.warnings}
    assert {
        "duplicate_news_rows",
        "malformed_news_rows",
        "future_news_rows",
        "missing_news_summaries",
    } <= warning_codes


def test_finnhub_discovery_fingerprint_is_stable_across_retrieval_times() -> None:
    from eventlens.providers import NewsArticleMetadata, NewsSummaryStatus

    values = {
        "ticker": "DEMO",
        "provider_article_id": "finnhub-news-1",
        "headline": "Headline",
        "provider_summary": "Summary",
        "summary_status": NewsSummaryStatus.RETAINED_PERMITTED,
        "url": "https://example.com/story",
        "published_at": datetime(2026, 7, 1, tzinfo=UTC),
    }
    first = NewsArticleMetadata(**values, retrieved_at=datetime(2026, 7, 2, tzinfo=UTC))
    second = NewsArticleMetadata(
        **values, retrieved_at=datetime(2026, 7, 3, tzinfo=UTC)
    )
    assert first.discovery_fingerprint == second.discovery_fingerprint
    assert first.item_sha256 != second.item_sha256


def test_finnhub_malformed_related_value_does_not_abort_response(
    tmp_path: Path,
) -> None:
    published = int(datetime(2026, 7, 1, 12, tzinfo=UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "datetime": published,
                    "headline": "Bad related",
                    "id": 1,
                    "related": ["DEMO"],
                    "url": "https://example.com/bad",
                }
            ],
        )

    result = FinnhubProvider("key", _client(tmp_path, handler)).get_company_news(
        "DEMO",
        date(2026, 7, 1),
        date(2026, 7, 2),
    )
    assert result.data.articles == ()
    assert "malformed_news_rows" in {item.code for item in result.provenance.warnings}


def test_finnhub_out_of_range_timestamp_is_a_malformed_row(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "datetime": 10**30,
                    "headline": "Impossible date",
                    "id": 1,
                    "related": "DEMO",
                    "url": "https://example.com/bad-date",
                }
            ],
        )

    result = FinnhubProvider("key", _client(tmp_path, handler)).get_company_news(
        "DEMO",
        date(2026, 7, 1),
        date(2026, 7, 2),
    )
    assert result.data.articles == ()
    assert "malformed_news_rows" in {item.code for item in result.provenance.warnings}


def test_finnhub_news_rejects_cutoff_after_query_end_before_http(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = FinnhubProvider("key", _client(tmp_path, handler))

    with pytest.raises(ValueError, match="cutoff_at must not be after query_end"):
        provider.get_company_news(
            "DEMO",
            date(2026, 7, 1),
            date(2026, 7, 2),
            cutoff_at=datetime(2026, 7, 3, tzinfo=UTC),
        )

    assert calls == 0


def test_finnhub_news_requires_utc_cutoff_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    provider = FinnhubProvider("key", _client(tmp_path, handler))
    with pytest.raises(ValueError, match="cutoff_at must use UTC"):
        provider.get_company_news(
            "DEMO",
            date(2026, 7, 1),
            date(2026, 7, 2),
            cutoff_at=datetime(2026, 7, 2, 17, tzinfo=UTC).astimezone(
                timezone(timedelta(hours=-4))
            ),
        )
    assert calls == 0


def test_finnhub_rejects_history_outside_free_month_without_spending_quota(
    tmp_path: Path,
) -> None:
    http = _client(tmp_path, lambda request: httpx.Response(500))
    provider = FinnhubProvider("key", http, today=lambda: date(2026, 7, 18))

    with pytest.raises(ProviderEntitlementError, match="latest month"):
        provider.get_earnings_events("DEMO", date(2026, 5, 1), date(2026, 5, 31))

    assert http.ledger.usage("finnhub") == 0


def test_finnhub_news_rejects_cache_miss_beyond_free_history_without_spending_quota(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    http = _client(tmp_path, handler)
    provider = FinnhubProvider("key", http, today=lambda: date(2026, 7, 18))

    with pytest.raises(ProviderEntitlementError, match="cache-only"):
        provider.get_company_news("DEMO", date(2024, 1, 1), date(2024, 1, 31))

    assert calls == 0
    assert http.ledger.usage("finnhub") == 0


def test_finnhub_preserves_cached_window_after_it_ages_out_of_free_history(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"earningsCalendar": []})

    http = _client(tmp_path, handler)
    start, end = date(2026, 5, 1), date(2026, 5, 31)
    FinnhubProvider("key", http, today=lambda: date(2026, 5, 31)).get_earnings_events(
        "DEMO", start, end
    )
    cached = FinnhubProvider(
        "key", http, today=lambda: date(2026, 7, 18)
    ).get_earnings_events("DEMO", start, end)

    assert calls == 1
    assert cached.provenance.origin.value == "cache"
    assert cached.provenance.quota_consumed is False


def test_corrupt_quota_ledger_fails_closed_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"quarterlyEarnings": []})

    http = _client(tmp_path, handler)
    http.ledger.path.parent.mkdir(parents=True, exist_ok=True)
    http.ledger.path.write_text("not-json", encoding="utf-8")
    provider = AlphaVantageProvider("key", http)

    with pytest.raises(ProviderQuotaExceededError, match="unreadable"):
        provider.get_earnings_events("DEMO", date(2026, 1, 1), date(2026, 7, 1))

    assert calls == 0


def test_sec_item_202_filing_is_no_key_historical_evidence(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "EventLens test@eventlens.dev" == request.headers["User-Agent"]
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(
                200,
                json={
                    "0": {
                        "cik_str": 789019,
                        "ticker": "DEMO",
                        "title": "example systems",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "filings": {
                    "recent": {
                        "accessionNumber": ["0001193125-26-000001", "ignored"],
                        "filingDate": ["2026-04-29", "2026-03-01"],
                        "form": ["8-K", "10-Q"],
                        "items": ["2.02,9.01", ""],
                    }
                }
            },
        )

    provider = SecEdgarProvider(
        "EventLens test@eventlens.dev", _client(tmp_path, handler)
    )
    result = provider.get_earnings_filings("DEMO", date(2026, 4, 1), date(2026, 5, 1))

    assert result.data.filings[0].accession_number == "0001193125-26-000001"
    assert result.data.filings[0].filing_date == date(2026, 4, 29)
    assert result.provenance.quota_consumed is False


def test_sec_requests_are_spaced_below_fair_access_ceiling(tmp_path: Path) -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(
                200,
                json={"0": {"cik_str": 789019, "ticker": "DEMO"}},
            )
        return httpx.Response(
            200,
            json={
                "filings": {
                    "recent": {
                        "accessionNumber": [],
                        "filingDate": [],
                        "form": [],
                        "items": [],
                    }
                }
            },
        )

    http = CachedHttpClient(
        tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limit_epoch=lambda: 100.0,
        rate_limit_sleeper=sleeps.append,
    )
    provider = SecEdgarProvider("EventLens ops@eventlens.dev", http)

    provider.get_earnings_filings("DEMO", date(2026, 1, 1), date(2026, 1, 2))

    assert sleeps == pytest.approx([0.11])


def test_sec_placeholder_identity_is_rejected_and_registry_disables_it(
    tmp_path: Path,
) -> None:
    http = _client(tmp_path, lambda request: httpx.Response(500))

    with pytest.raises(ProviderConfigurationError, match="real contact email"):
        SecEdgarProvider("EventLens contact@example.com", http)

    registry = build_free_first_registry(
        EventLensSettings.model_validate({"sec_user_agent": ""}), http=http
    )
    sec_plan = next(item for item in registry.plan() if item.provider == "SEC EDGAR")
    assert registry.sec is None
    assert sec_plan.enabled is False


def test_yfinance_injected_loader_preserves_adjusted_close() -> None:
    provider = YFinanceProvider(
        lambda ticker, start, end: [
            {
                "date": "2026-07-01",
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "adjusted_close": "100.50",
                "volume": 50,
            }
        ]
    )

    result = provider.get_daily_history("demo", date(2026, 7, 1), date(2026, 7, 1))

    assert str(result.data.bars[0].adjusted_close) == "100.50"
    assert result.provenance.quota_consumed is False
    assert result.provenance.warnings[0].code == "normalized_adapter_checksum"


def test_fixture_and_registry_make_modes_explicit(tmp_path: Path) -> None:
    fixture = {
        "schema_version": "1.0",
        "ticker": "DEMO",
        "retrieved_at": "2026-07-18T12:00:00+00:00",
        "market_history": {
            "ticker": "DEMO",
            "bars": [
                {
                    "trading_date": "2026-07-01",
                    "open": "100",
                    "high": "102",
                    "low": "99",
                    "close": "101",
                    "adjusted_close": "101",
                    "volume": 50,
                }
            ],
        },
        "earnings_events": {"ticker": "DEMO", "events": []},
        "transcripts": {},
        "option_snapshots": {},
    }
    (tmp_path / "DEMO.json").write_text(json.dumps(fixture), encoding="utf-8")
    fixture_result = FixtureProvider(tmp_path).get_daily_history(
        "DEMO", date(2026, 7, 1), date(2026, 7, 1)
    )
    repeated_fixture_result = FixtureProvider(tmp_path).get_daily_history(
        "DEMO", date(2026, 7, 1), date(2026, 7, 1)
    )
    settings = EventLensSettings.model_validate(
        {
            "alpha_vantage_api_key": "alpha",
            "finnhub_api_key": "finn",
            "massive_api_key": "massive",
            "sec_user_agent": "EventLens test@eventlens.dev",
        }
    )
    registry = build_free_first_registry(settings, fixture_root=tmp_path)

    assert fixture_result.provenance.origin.value == "fixture"
    assert repeated_fixture_result.provenance == fixture_result.provenance
    assert registry.alpha_vantage is not None
    assert registry.finnhub is not None
    assert registry.massive is not None
    assert {item.provider for item in registry.plan() if item.enabled} >= {
        "Alpha Vantage",
        "Finnhub",
        "Massive",
        "SEC EDGAR",
        "yfinance",
        "fixture",
    }
