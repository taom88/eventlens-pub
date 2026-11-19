"""Quota-aware Alpha Vantage adapter for free and explicitly premium endpoints."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from eventlens.domain.events import EventTiming

from ._common import (
    decimal_or_none,
    normalized_ticker,
    provenance,
    require_mapping,
    strict_utc_cutoff,
)
from .errors import ProviderEntitlementError, ProviderPayloadError
from .http import CachedHttpClient, HttpFetch
from .models import (
    DailyBar,
    CompanyNewsMetadata,
    DataQualityStatus,
    EarningsEventRecord,
    EarningsEvents,
    MarketHistory,
    NewsArticleMetadata,
    ProviderResponse,
    ProviderWarning,
    TranscriptPassage,
    TranscriptRecord,
)


class AlphaVantageProvider:
    provider_name = "alpha_vantage"
    provider_version = "2026-07"
    base_url = "https://www.alphavantage.co/query"

    def __init__(
        self,
        api_key: str,
        http: CachedHttpClient,
        *,
        daily_budget: int = 20,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Alpha Vantage API key must not be blank")
        self._api_key = api_key
        self._http = http
        self._daily_budget = daily_budget

    def get_daily_history(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[MarketHistory]:
        symbol = normalized_ticker(ticker)
        if end < start:
            raise ValueError("end must be on or after start")
        fetch = self._query(
            function="TIME_SERIES_DAILY",
            parameters={"symbol": symbol, "outputsize": "compact"},
            max_age_seconds=6 * 60 * 60,
        )
        payload = self._validated_payload(fetch)
        series = payload.get("Time Series (Daily)")
        if not isinstance(series, dict):
            raise ProviderPayloadError("Alpha Vantage daily response omitted Time Series (Daily)")
        bars = tuple(
            DailyBar(
                trading_date=day,
                open=Decimal(str(row["1. open"])),
                high=Decimal(str(row["2. high"])),
                low=Decimal(str(row["3. low"])),
                close=Decimal(str(row["4. close"])),
                volume=int(row["5. volume"]),
            )
            for value, row in sorted(series.items())
            if start <= (day := date.fromisoformat(value)) <= end and isinstance(row, dict)
        )
        warnings: tuple[ProviderWarning, ...] = (
            ProviderWarning(
                code="unadjusted_daily_prices",
                message=(
                    "Free TIME_SERIES_DAILY is unadjusted; do not use it for the "
                    "canonical realized-volatility proxy when corporate actions may matter."
                ),
            ),
        )
        quality = DataQualityStatus.DEGRADED
        if start < date.today() and (not bars or bars[0].trading_date > start):
            quality = DataQualityStatus.PARTIAL
            warnings = warnings + (
                ProviderWarning(
                    code="compact_history_window",
                    message="Free Alpha Vantage daily history contains only the latest 100 points.",
                ),
            )
        return ProviderResponse(
            data=MarketHistory(ticker=symbol, bars=bars),
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=quality,
                coverage_start=bars[0].trading_date if bars else None,
                coverage_end=bars[-1].trading_date if bars else None,
                warnings=warnings,
            ),
        )

    def get_earnings_events(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[EarningsEvents]:
        symbol = normalized_ticker(ticker)
        if end < start:
            raise ValueError("end must be on or after start")
        fetch = self._query(
            function="EARNINGS",
            parameters={"symbol": symbol},
            max_age_seconds=7 * 24 * 60 * 60,
        )
        payload = self._validated_payload(fetch)
        rows = payload.get("quarterlyEarnings")
        if not isinstance(rows, list):
            raise ProviderPayloadError("Alpha Vantage earnings response omitted quarterlyEarnings")
        events: list[EarningsEventRecord] = []
        timing_unavailable = False
        for row in rows:
            if not isinstance(row, dict) or not row.get("reportedDate"):
                continue
            event_date = date.fromisoformat(str(row["reportedDate"]))
            if start <= event_date <= end:
                fiscal_end = str(row.get("fiscalDateEnding") or "")
                timing = _event_timing(row.get("reportTime"))
                timing_unavailable = (
                    timing_unavailable or timing is EventTiming.UNKNOWN
                )
                events.append(
                    EarningsEventRecord(
                        ticker=symbol,
                        event_date=event_date,
                        fiscal_period_end=(
                            date.fromisoformat(fiscal_end) if fiscal_end else None
                        ),
                        timing=timing,
                        source_event_id=f"{symbol}:{event_date.isoformat()}:{fiscal_end}",
                        reported_eps=decimal_or_none(row.get("reportedEPS")),
                        estimated_eps=decimal_or_none(row.get("estimatedEPS")),
                    )
                )
        events.sort(key=lambda item: item.event_date)
        return ProviderResponse(
            data=EarningsEvents(ticker=symbol, events=tuple(events)),
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=DataQualityStatus.COMPLETE,
                coverage_start=events[0].event_date if events else None,
                coverage_end=events[-1].event_date if events else None,
                warnings=(
                    (
                        ProviderWarning(
                            code="event_timing_unavailable",
                            message=(
                                "One or more Alpha Vantage earnings rows omitted a "
                                "recognized pre-market/post-market reportTime."
                            ),
                        ),
                    )
                    if timing_unavailable
                    else ()
                ),
            ),
        )

    def get_transcript(
        self, ticker: str, fiscal_quarter: str
    ) -> ProviderResponse[TranscriptRecord]:
        symbol = normalized_ticker(ticker)
        quarter = fiscal_quarter.strip().upper()
        if len(quarter) != 6 or quarter[4] != "Q" or quarter[5] not in "1234":
            raise ValueError("fiscal_quarter must use YYYYQ1 through YYYYQ4")
        fetch = self._query(
            function="EARNINGS_CALL_TRANSCRIPT",
            parameters={"symbol": symbol, "quarter": quarter},
            max_age_seconds=None,
        )
        payload = self._validated_payload(fetch)
        rows = payload.get("transcript")
        if not isinstance(rows, list):
            raise ProviderPayloadError("Alpha Vantage transcript response omitted transcript")
        passages: list[TranscriptPassage] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            text = str(row.get("content") or row.get("text") or "").strip()
            if not text:
                continue
            digest = hashlib.sha256(
                f"{symbol}|{quarter}|{index}|{text}".encode("utf-8")
            ).hexdigest()[:20]
            passages.append(
                TranscriptPassage(
                    passage_id=f"av-{digest}",
                    speaker=_string_or_none(row.get("speaker")),
                    role=_string_or_none(row.get("title")),
                    section=_string_or_none(row.get("section")),
                    company=_string_or_none(row.get("company")),
                    firm=_string_or_none(row.get("firm")),
                    text=text,
                )
            )
        if not passages:
            raise ProviderPayloadError("Alpha Vantage transcript contained no usable passages")
        event_date = _parse_optional_date(payload.get("date"))
        return ProviderResponse(
            data=TranscriptRecord(
                ticker=symbol,
                fiscal_quarter=quarter,
                event_date=event_date,
                passages=tuple(passages),
            ),
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=DataQualityStatus.COMPLETE,
                coverage_start=event_date,
                coverage_end=event_date,
            ),
        )

    def get_company_news(
        self, ticker: str, start: date, end: date, *, cutoff_at: datetime | None = None
    ) -> ProviderResponse[CompanyNewsMetadata]:
        symbol = normalized_ticker(ticker)
        if end < start:
            raise ValueError("end must be on or after start")
        if (end - start).days > 31:
            raise ValueError("news requests must be split into <=31-day windows")
        cutoff = strict_utc_cutoff(
            cutoff_at or datetime.combine(end, time.max, tzinfo=timezone.utc)
        )
        start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
        if start_at > cutoff:
            raise ValueError("start must be before cutoff_at")
        if cutoff.date() > end:
            raise ValueError("cutoff_at must not be after query_end")
        fetch = self._query(
            function="NEWS_SENTIMENT",
            parameters={
                "tickers": symbol,
                "time_from": _alpha_news_time(start, time.min),
                "time_to": _alpha_news_time(cutoff.date(), cutoff.timetz().replace(tzinfo=None)),
                "sort": "EARLIEST",
                "limit": "1000",
            },
            max_age_seconds=6 * 60 * 60,
        )
        payload = self._validated_payload(fetch)
        rows = payload.get("feed")
        if not isinstance(rows, list):
            raise ProviderPayloadError("Alpha Vantage news response omitted feed")

        articles: list[NewsArticleMetadata] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            published_at = _parse_alpha_news_datetime(row.get("time_published"))
            if published_at is None or not (
                start_at <= published_at < cutoff
            ):
                continue
            url = _string_or_none(row.get("url"))
            headline = _string_or_none(row.get("title"))
            if url is None or headline is None:
                continue
            ticker_sentiment = _alpha_ticker_sentiment(row.get("ticker_sentiment"), symbol)
            article_id = hashlib.sha256(
                f"alpha_vantage|{symbol}|{published_at.isoformat()}|{url}".encode("utf-8")
            ).hexdigest()[:24]
            articles.append(
                NewsArticleMetadata(
                    ticker=symbol,
                    provider_article_id=f"av-news-{article_id}",
                    headline=headline,
                    source_name=_string_or_none(row.get("source")),
                    source_domain=_source_domain_or_none(row.get("source_domain")),
                    url=url,
                    published_at=published_at,
                    category=_string_or_none(row.get("category_within_source")),
                    related_tickers=_alpha_related_tickers(row.get("ticker_sentiment")),
                    overall_sentiment_score=decimal_or_none(row.get("overall_sentiment_score")),
                    overall_sentiment_label=_string_or_none(row.get("overall_sentiment_label")),
                    ticker_relevance_score=ticker_sentiment.get("ticker_relevance_score"),
                    ticker_sentiment_score=ticker_sentiment.get("ticker_sentiment_score"),
                    ticker_sentiment_label=ticker_sentiment.get("ticker_sentiment_label"),
                )
            )
        articles.sort(key=lambda item: (item.published_at, item.provider_article_id))
        return ProviderResponse(
            data=CompanyNewsMetadata(ticker=symbol, articles=tuple(articles)),
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=DataQualityStatus.COMPLETE,
                coverage_start=articles[0].published_at.date() if articles else None,
                coverage_end=articles[-1].published_at.date() if articles else None,
                warnings=(
                    ProviderWarning(
                        code="metadata_only_news",
                        message=(
                            "Alpha Vantage NEWS_SENTIMENT summaries and article bodies are "
                            "not retained; only rights-safe metadata and sentiment fields are exposed."
                        ),
                    ),
                ),
            ),
        )

    def get_historical_options(
        self, ticker: str, snapshot_date: date
    ) -> None:
        del ticker, snapshot_date
        raise ProviderEntitlementError(
            "Alpha Vantage HISTORICAL_OPTIONS is premium and is not integrated in the "
            "free-first MVP; use a reviewed fixture"
        )

    def _query(
        self,
        *,
        function: str,
        parameters: dict[str, str],
        max_age_seconds: int | None,
    ) -> HttpFetch:
        return self._http.get_json(
            provider=self.provider_name,
            url=self.base_url,
            parameters={"function": function, **parameters, "apikey": self._api_key},
            secret_parameter_names=frozenset({"apikey"}),
            daily_budget=self._daily_budget,
            max_age_seconds=max_age_seconds,
            payload_validator=self._ensure_success_payload,
        )

    @staticmethod
    def _validated_payload(fetch: HttpFetch) -> dict[str, Any]:
        payload = require_mapping(fetch.payload, "Alpha Vantage")
        AlphaVantageProvider._ensure_success_payload(payload)
        return payload

    @staticmethod
    def _ensure_success_payload(value: Any) -> None:
        payload = require_mapping(value, "Alpha Vantage")
        for key in ("Error Message", "Information", "Note"):
            message = payload.get(key)
            if message:
                # Preserve the provider's bounded diagnostic.  This is useful for
                # distinguishing quota/entitlement responses from malformed data,
                # without ever including the API key.
                detail = " ".join(str(message).split())[:240]
                raise ProviderPayloadError(
                    f"Alpha Vantage returned {key}: request unavailable ({detail})"
                )


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source_domain_or_none(value: Any) -> str | None:
    """Normalize Alpha's occasional URL-shaped source_domain field.

    Alpha Vantage normally returns a bare domain, but cached/free payloads can
    contain ``https://...``.  The rights-safe model intentionally accepts only
    a domain label, so normalize the provider variation at the adapter edge.
    """
    text = _string_or_none(value)
    if text is None:
        return None
    parsed = urlparse(text if "://" in text else f"//{text}")
    return parsed.netloc or text.split("/", 1)[0]


def _parse_optional_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _event_timing(value: Any) -> EventTiming:
    return {
        "pre-market": EventTiming.BEFORE_MARKET_OPEN,
        "post-market": EventTiming.AFTER_MARKET_CLOSE,
    }.get(str(value or "").strip().casefold(), EventTiming.UNKNOWN)


def _alpha_news_time(day: date, clock: time) -> str:
    return datetime.combine(day, clock).strftime("%Y%m%dT%H%M")


def _parse_alpha_news_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _alpha_related_tickers(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    related: list[str] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        ticker = _string_or_none(row.get("ticker"))
        if ticker is not None:
            normalized = normalized_ticker(ticker)
            if normalized not in related:
                related.append(normalized)
    return tuple(related)


def _alpha_ticker_sentiment(
    value: Any,
    symbol: str,
) -> dict[str, Decimal | str | None]:
    if not isinstance(value, list):
        return {}
    for row in value:
        if not isinstance(row, dict):
            continue
        ticker = _string_or_none(row.get("ticker"))
        if ticker is not None and normalized_ticker(ticker) == symbol:
            return {
                "ticker_relevance_score": decimal_or_none(row.get("relevance_score")),
                "ticker_sentiment_score": decimal_or_none(row.get("ticker_sentiment_score")),
                "ticker_sentiment_label": _string_or_none(row.get("ticker_sentiment_label")),
            }
    return {}
