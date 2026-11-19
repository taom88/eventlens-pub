"""Finnhub free-tier adapter focused on quota-efficient earnings metadata."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any

from eventlens.domain.events import EventTiming

from ._common import (
    decimal_or_none,
    normalized_ticker,
    provenance,
    require_mapping,
    strict_utc_cutoff,
)
from .errors import ProviderEntitlementError, ProviderPayloadError
from .http import CachedHttpClient, ProviderHTTPError
from .models import (
    DataQualityStatus,
    EarningsEventRecord,
    EarningsEvents,
    CompanyNewsMetadata,
    MarketHistory,
    NewsTickerMatch,
    NewsSummaryStatus,
    NewsArticleMetadata,
    ProviderResponse,
    ProviderWarning,
    TranscriptRecord,
)


class FinnhubProvider:
    provider_name = "finnhub"
    provider_version = "2026-07"
    base_url = "https://finnhub.io/api/v1"
    minimum_interval_seconds = 1.05

    def __init__(
        self,
        api_key: str,
        http: CachedHttpClient,
        *,
        daily_budget: int = 50,
        today: Callable[[], date] | None = None,
        allow_provider_summary_retention: bool = False,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Finnhub API key must not be blank")
        self._api_key = api_key
        self._http = http
        self._daily_budget = daily_budget
        self._today = today or date.today
        self._allow_provider_summary_retention = allow_provider_summary_retention

    def get_earnings_events(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[EarningsEvents]:
        symbol = normalized_ticker(ticker)
        if end < start:
            raise ValueError("end must be on or after start")
        if (end - start).days > 31:
            raise ValueError("Finnhub free calendar requests must be split into <=31-day windows")
        today = self._today()
        outside_free_history = start < today - timedelta(days=31)
        try:
            fetch = self._http.get_json(
                provider=self.provider_name,
                url=f"{self.base_url}/calendar/earnings",
                parameters={
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "symbol": symbol,
                    "international": "false",
                    "token": self._api_key,
                },
                secret_parameter_names=frozenset({"token"}),
                daily_budget=self._daily_budget,
                max_age_seconds=None if end < today else 6 * 60 * 60,
                payload_validator=_validate_finnhub_payload,
                minimum_interval_seconds=self.minimum_interval_seconds,
                live_request_allowed=not outside_free_history,
            )
        except ProviderHTTPError as error:
            if outside_free_history:
                raise ProviderEntitlementError(
                    "Finnhub free earnings-calendar history is limited to the latest month "
                    "and no cached response exists; use Alpha Vantage or SEC evidence"
                ) from error
            raise
        payload = require_mapping(fetch.payload, "Finnhub")
        rows = payload.get("earningsCalendar")
        if not isinstance(rows, list):
            raise ProviderPayloadError("Finnhub response omitted earningsCalendar")
        events: list[EarningsEventRecord] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("date"):
                continue
            event_date = date.fromisoformat(str(row["date"]))
            if start <= event_date <= end and str(row.get("symbol", symbol)).upper() == symbol:
                year = row.get("year")
                quarter = row.get("quarter")
                fiscal_quarter = f"{year}Q{quarter}" if year and quarter else None
                events.append(
                    EarningsEventRecord(
                        ticker=symbol,
                        event_date=event_date,
                        fiscal_quarter=fiscal_quarter,
                        timing=_event_timing(row.get("hour")),
                        source_event_id=f"{symbol}:{event_date.isoformat()}:{fiscal_quarter or 'unknown'}",
                        reported_eps=decimal_or_none(row.get("epsActual")),
                        estimated_eps=decimal_or_none(row.get("epsEstimate")),
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
                coverage_start=start,
                coverage_end=end,
                warnings=(
                    ProviderWarning(
                        code="free_tier_one_month",
                        message="Finnhub free earnings-calendar history is limited to one month.",
                    ),
                ),
            ),
        )

    def get_daily_history(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[MarketHistory]:
        raise ProviderEntitlementError(
            "Finnhub stock candles are premium; EventLens uses yfinance for free OHLCV"
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
            cutoff_at or datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc)
        )
        start_at = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        if start_at > cutoff:
            raise ValueError("start must be before cutoff_at")
        if cutoff.date() > end:
            raise ValueError("cutoff_at must not be after query_end")
        outside_free_history = start < self._today() - timedelta(days=365)
        try:
            fetch = self._http.get_json(
                provider=self.provider_name,
                url=f"{self.base_url}/company-news",
                parameters={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": self._api_key},
                secret_parameter_names=frozenset({"token"}), daily_budget=self._daily_budget,
                max_age_seconds=None if outside_free_history else 6 * 60 * 60,
                payload_validator=_validate_finnhub_news_payload,
                minimum_interval_seconds=self.minimum_interval_seconds,
                live_request_allowed=not outside_free_history,
            )
        except ProviderHTTPError as error:
            if outside_free_history:
                raise ProviderEntitlementError("Finnhub company-news history is cache-only beyond one year") from error
            raise
        if not isinstance(fetch.payload, list):
            raise ProviderPayloadError("Finnhub company-news response must be a JSON array")
        articles: list[NewsArticleMetadata] = []
        malformed_rows = future_rows = missing_summaries = duplicate_rows = 0
        seen_ids: set[str] = set()
        seen_fingerprints: set[str] = set()
        for row in fetch.payload:
            if not isinstance(row, dict):
                malformed_rows += 1
                continue
            published_at = _parse_finnhub_news_datetime(row.get("datetime"))
            if published_at is None:
                malformed_rows += 1
                continue
            if published_at >= cutoff:
                future_rows += 1
                continue
            if published_at < start_at:
                continue
            headline = _string_or_none(row.get("headline"))
            url = _string_or_none(row.get("url"))
            article_id = _string_or_none(row.get("id"))
            if headline is None or url is None or article_id is None:
                malformed_rows += 1
                continue
            provider_article_id = f"finnhub-news-{article_id}"
            if provider_article_id in seen_ids:
                duplicate_rows += 1
                continue
            try:
                related = _finnhub_related_tickers(row.get("related"))
                ticker_match = _ticker_match(symbol, related)
                supplied_summary = _string_or_none(row.get("summary"))
                summary = supplied_summary if self._allow_provider_summary_retention else None
                summary_status = (
                    NewsSummaryStatus.MISSING_FROM_PROVIDER if supplied_summary is None
                    else NewsSummaryStatus.RETAINED_PERMITTED if self._allow_provider_summary_retention
                    else NewsSummaryStatus.WITHHELD_NO_PERMISSION
                )
                article = NewsArticleMetadata(
                    ticker=symbol,
                    provider_article_id=provider_article_id,
                    headline=headline,
                    provider_summary=summary,
                    summary_status=summary_status,
                    source_name=_string_or_none(row.get("source")),
                    url=url,
                    published_at=published_at,
                    retrieved_at=fetch.retrieved_at,
                    category=_string_or_none(row.get("category")),
                    related_tickers=related,
                    ticker_match=ticker_match,
                    provider_fields={
                        key: value for key in ("category", "related")
                        if (value := row.get(key)) is None or isinstance(value, (str, int, float, bool))
                    },
                )
            except ValueError:
                malformed_rows += 1
                continue
            if article.discovery_fingerprint in seen_fingerprints:
                duplicate_rows += 1
                continue
            seen_ids.add(provider_article_id)
            seen_fingerprints.add(article.discovery_fingerprint or "")
            if supplied_summary is None:
                missing_summaries += 1
            articles.append(article)
        articles.sort(key=lambda item: (item.published_at, item.provider_article_id))
        warnings = [
            ProviderWarning(
                code="discovery_summary_not_body",
                message=(
                    "Finnhub provider summaries are retained only when explicit permission is configured; "
                    "they remain discovery metadata and must not be presented as article bodies."
                ),
            )
        ]
        for code, count, label in (
            ("malformed_news_rows", malformed_rows, "malformed"),
            ("future_news_rows", future_rows, "at-or-after-cutoff"),
            ("duplicate_news_rows", duplicate_rows, "duplicate"),
            ("missing_news_summaries", missing_summaries, "summary-missing"),
        ):
            if count:
                warnings.append(ProviderWarning(code=code, message=f"Excluded or flagged {count} {label} Finnhub news row(s)."))
        return ProviderResponse(
            data=CompanyNewsMetadata(ticker=symbol, articles=tuple(articles)),
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=DataQualityStatus.COMPLETE,
                coverage_start=articles[0].published_at.date() if articles else None,
                coverage_end=articles[-1].published_at.date() if articles else None,
                warnings=tuple(warnings),
            ),
        )

    def get_transcript(
        self, ticker: str, fiscal_quarter: str
    ) -> ProviderResponse[TranscriptRecord]:
        raise ProviderEntitlementError(
            "Finnhub earnings transcripts are premium; EventLens uses Alpha Vantage first"
        )


def _event_timing(value: Any) -> EventTiming:
    return {
        "bmo": EventTiming.BEFORE_MARKET_OPEN,
        "amc": EventTiming.AFTER_MARKET_CLOSE,
        "dmh": EventTiming.DURING_MARKET_HOURS,
    }.get(str(value or "").lower(), EventTiming.UNKNOWN)


def _validate_finnhub_payload(value: Any) -> None:
    payload = require_mapping(value, "Finnhub")
    if payload.get("error"):
        raise ProviderPayloadError("Finnhub returned an endpoint or entitlement error")


def _validate_finnhub_news_payload(value: Any) -> None:
    if isinstance(value, dict) and value.get("error"):
        raise ProviderPayloadError("Finnhub returned an endpoint or entitlement error")
    if not isinstance(value, list):
        raise ProviderPayloadError("Finnhub company-news response must be a JSON array")


def _parse_finnhub_news_datetime(value: Any) -> datetime | None:
    try:
        timestamp = int(value)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _finnhub_related_tickers(value: Any) -> tuple[str, ...]:
    text = _string_or_none(value)
    if text is None:
        return ()
    tickers = {
        normalized_ticker(item)
        for item in re.split(r"[,;|\s]+", text)
        if item.strip()
    }
    return tuple(sorted(tickers))


def _ticker_match(symbol: str, related: tuple[str, ...]) -> NewsTickerMatch:
    if not related:
        return NewsTickerMatch.UNKNOWN
    if related == (symbol,):
        return NewsTickerMatch.EXACT
    if symbol in related:
        return NewsTickerMatch.RELATED
    return NewsTickerMatch.MISMATCH
