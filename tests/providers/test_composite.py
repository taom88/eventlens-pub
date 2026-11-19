from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from eventlens.domain.events import EventTiming
from eventlens.providers import (
    CompanyNewsMetadata,
    DataQualityStatus,
    EarningsEventRecord,
    EarningsEvents,
    FreeFirstEventMetadataProvider,
    FreeFirstNewsProvider,
    NewsArticleMetadata,
    ProviderOrigin,
    ProviderProvenance,
    ProviderResponse,
)
from eventlens.providers.errors import ProviderPayloadError


class StubEvents:
    def __init__(self, name, events):
        self.provider_name = name
        self.events = events
        self.calls = []

    def get_earnings_events(self, ticker, start, end):
        self.calls.append((ticker, start, end))
        digest = hashlib.sha256(f"{self.provider_name}:{start}:{end}".encode()).hexdigest()
        return ProviderResponse(
            data=EarningsEvents(ticker=ticker, events=self.events),
            provenance=ProviderProvenance(
                provider_name=self.provider_name,
                provider_version="1",
                origin=ProviderOrigin.LIVE,
                retrieved_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
                request_fingerprint=digest,
                raw_response_sha256=digest,
                quota_consumed=True,
                data_quality=DataQualityStatus.COMPLETE,
            ),
        )


class StubNews:
    provider_name = "stub_news"

    def __init__(self, name, articles=(), error: Exception | None = None):
        self.provider_name = name
        self.articles = articles
        self.error = error
        self.calls = []

    def get_company_news(self, ticker, start, end, *, cutoff_at=None):
        self.calls.append((ticker, start, end, cutoff_at))
        if self.error is not None:
            raise self.error
        digest = hashlib.sha256(
            f"{self.provider_name}:{start}:{end}:{cutoff_at}".encode()
        ).hexdigest()
        return ProviderResponse(
            data=CompanyNewsMetadata(ticker=ticker, articles=self.articles),
            provenance=ProviderProvenance(
                provider_name=self.provider_name,
                provider_version="1",
                origin=ProviderOrigin.FIXTURE,
                retrieved_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
                request_fingerprint=digest,
                raw_response_sha256=digest,
                quota_consumed=False,
                data_quality=DataQualityStatus.COMPLETE,
            ),
        )


def _event(day, event_id, timing=EventTiming.UNKNOWN):
    return EarningsEventRecord(
        ticker="DEMO", event_date=day, source_event_id=event_id, timing=timing
    )


def test_composite_uses_one_short_finnhub_window_and_prefers_its_timing() -> None:
    old = _event(date(2025, 10, 29), "alpha-old")
    alpha_duplicate = _event(date(2026, 7, 28), "alpha-current")
    finnhub_current = _event(
        date(2026, 7, 28), "finnhub-current", EventTiming.AFTER_MARKET_CLOSE
    )
    historical = StubEvents("alpha_vantage", (old, alpha_duplicate))
    near = StubEvents("finnhub", (finnhub_current,))
    provider = FreeFirstEventMetadataProvider(historical=historical, near_term=near)

    result = provider.get_earnings_events(
        "DEMO", date(2023, 1, 1), date(2026, 8, 3)
    )

    assert near.calls == [("DEMO", date(2026, 7, 3), date(2026, 8, 3))]
    assert [item.source_event_id for item in result.data.events] == [
        "alpha-old",
        "finnhub-current",
    ]
    assert result.data.events[-1].timing is EventTiming.AFTER_MARKET_CLOSE
    assert result.provenance.data_quality is DataQualityStatus.PARTIAL


def test_composite_is_complete_when_all_merged_event_timings_are_known() -> None:
    old = _event(
        date(2025, 10, 29), "alpha-old", EventTiming.AFTER_MARKET_CLOSE
    )
    current = _event(
        date(2026, 7, 28), "finnhub-current", EventTiming.AFTER_MARKET_CLOSE
    )
    provider = FreeFirstEventMetadataProvider(
        historical=StubEvents("alpha_vantage", (old,)),
        near_term=StubEvents("finnhub", (current,)),
    )

    result = provider.get_earnings_events(
        "DEMO", date(2023, 1, 1), date(2026, 8, 3)
    )

    assert result.provenance.data_quality is DataQualityStatus.COMPLETE


def test_news_composite_uses_primary_when_it_has_articles() -> None:
    article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="primary-1",
        headline="Primary headline",
        url="https://example.com/primary",
        published_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
    )
    primary = StubNews("alpha_vantage", (article,))
    fallback = StubNews("finnhub", ())
    provider = FreeFirstNewsProvider(primary=primary, fallback=fallback)
    cutoff = datetime(2026, 7, 2, 16, tzinfo=timezone.utc)

    result = provider.get_company_news("DEMO", date(2026, 7, 1), date(2026, 7, 2), cutoff_at=cutoff)

    assert result.data.articles == (article,)
    assert primary.calls == [("DEMO", date(2026, 7, 1), date(2026, 7, 2), cutoff)]
    assert fallback.calls == []
    assert result.provenance.provider_name == "alpha_vantage"


def test_news_composite_fallback_is_auditable_and_preserves_cutoff() -> None:
    article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="fallback-1",
        headline="Fallback headline",
        url="https://example.com/fallback",
        published_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
    )
    primary = StubNews("alpha_vantage", error=ProviderPayloadError("bad payload"))
    fallback = StubNews("finnhub", (article,))
    provider = FreeFirstNewsProvider(primary=primary, fallback=fallback)
    cutoff = datetime(2026, 7, 2, 16, tzinfo=timezone.utc)

    result = provider.get_company_news("DEMO", date(2026, 7, 1), date(2026, 7, 2), cutoff_at=cutoff)

    assert result.data.articles == (article,)
    assert primary.calls == [("DEMO", date(2026, 7, 1), date(2026, 7, 2), cutoff)]
    assert fallback.calls == [("DEMO", date(2026, 7, 1), date(2026, 7, 2), cutoff)]
    assert result.provenance.provider_name == "finnhub"
    assert result.provenance.warnings[-1].code == "news_fallback"
    assert "ProviderPayloadError" in result.provenance.warnings[-1].message
