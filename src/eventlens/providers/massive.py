"""Rights-safe Massive ticker-news adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from ._common import (
    decimal_or_none,
    normalized_ticker,
    provenance,
    require_mapping,
    strict_utc_cutoff,
)
from .errors import ProviderPayloadError
from .http import CachedHttpClient
from .models import (
    CompanyNewsMetadata,
    DailyTickerSummary,
    DataQualityStatus,
    NewsArticleMetadata,
    NewsSummaryStatus,
    NewsTickerMatch,
    ProviderResponse,
    ProviderWarning,
)


class MassiveSummaryRetentionGrant(BaseModel):
    """Explicit private-use grant for Massive descriptions and reasoning.

    The default adapter remains metadata-only.  Callers must supply this
    versioned grant before provider text enters normalized private evidence.
    It does not authorize redistribution or imply access to an article body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    grant_id: str = Field(min_length=1, max_length=128)
    grant_version: str = Field(min_length=1, max_length=64)
    authorized_use: Literal["private_internal_research"] = "private_internal_research"
    max_summary_characters: int = Field(default=10_000, ge=200, le=10_000)


class MassiveProvider:
    provider_name = "massive"
    provider_version = "2026-11"
    news_url = "https://api.massive.com/v2/reference/news"
    daily_summary_url = "https://api.massive.com/v1/open-close"
    minimum_interval_seconds = 12.1  # Free Stocks Basic: five calls/minute.

    def __init__(
        self, api_key: str, http: CachedHttpClient, *, daily_budget: int = 20
    ) -> None:
        if not api_key.strip():
            raise ValueError("Massive API key must not be blank")
        self._api_key = api_key
        self._http = http
        self._daily_budget = daily_budget

    def get_company_news(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        cutoff_at: datetime | None = None,
        cache_only: bool = False,
        summary_retention_grant: MassiveSummaryRetentionGrant | None = None,
    ) -> ProviderResponse[CompanyNewsMetadata]:
        symbol = normalized_ticker(ticker)
        if end < start:
            raise ValueError("end must be on or after start")
        cutoff = strict_utc_cutoff(
            cutoff_at or datetime.combine(end, time.max, tzinfo=UTC)
        )
        start_at = datetime.combine(start, time.min, tzinfo=UTC)
        if start_at > cutoff:
            raise ValueError("start must be before cutoff_at")
        if cutoff.date() > end:
            raise ValueError("cutoff_at must not be after query_end")
        fetch = self._http.get_json(
            provider=self.provider_name,
            url=self.news_url,
            parameters={
                "ticker": symbol,
                "published_utc.gte": _rfc3339(start_at),
                "published_utc.lt": _rfc3339(cutoff),
                "order": "asc",
                "sort": "published_utc",
                "limit": "1000",
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
            daily_budget=self._daily_budget,
            max_age_seconds=None if end < date.today() else 60 * 60,
            minimum_interval_seconds=self.minimum_interval_seconds,
            payload_validator=_validate_payload,
            live_request_allowed=not cache_only,
        )
        payload = require_mapping(fetch.payload, "Massive")
        if payload.get("next_url"):
            raise ProviderPayloadError(
                "Massive news exceeded the bounded 1000-row window; split the query"
            )
        rows = payload["results"]
        articles: list[NewsArticleMetadata] = []
        malformed = future = duplicates = unknown_sentiment = 0
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                malformed += 1
                continue
            article_id = _text(row.get("id"))
            headline = _text(row.get("title"))
            url = _absolute_url(row.get("article_url"))
            published_at = _timestamp(row.get("published_utc"))
            if not all((article_id, headline, url, published_at)):
                malformed += 1
                continue
            if published_at < start_at:
                continue
            if published_at >= cutoff:
                future += 1
                continue
            if article_id in seen:
                duplicates += 1
                continue
            seen.add(article_id)
            related = _tickers(row.get("tickers"))
            sentiment, sentiment_reasoning = _ticker_insight(
                row.get("insights"), symbol
            )
            if sentiment is not None and sentiment not in {
                "positive",
                "neutral",
                "negative",
            }:
                sentiment = None
                unknown_sentiment += 1
            publisher = (
                row.get("publisher") if isinstance(row.get("publisher"), dict) else {}
            )
            homepage = _absolute_url(publisher.get("homepage_url"))
            description = _text(row.get("description"))
            provider_summary = _provider_summary(
                description=description,
                sentiment_reasoning=sentiment_reasoning,
                grant=summary_retention_grant,
            )
            articles.append(
                NewsArticleMetadata(
                    ticker=symbol,
                    provider_article_id=f"massive-news-{article_id}",
                    headline=headline,
                    provider_summary=provider_summary,
                    summary_status=(
                        NewsSummaryStatus.RETAINED_PERMITTED
                        if provider_summary is not None
                        else NewsSummaryStatus.WITHHELD_NO_PERMISSION
                        if description or sentiment_reasoning
                        else NewsSummaryStatus.MISSING_FROM_PROVIDER
                    ),
                    source_name=_text(publisher.get("name")),
                    source_domain=_domain(homepage or url),
                    url=url,
                    published_at=published_at,
                    retrieved_at=fetch.retrieved_at,
                    related_tickers=related,
                    ticker_match=_ticker_match(symbol, related),
                    ticker_sentiment_label=sentiment,
                    provider_fields=(
                        {
                            "summary_grant_id": summary_retention_grant.grant_id,
                            "summary_grant_version": summary_retention_grant.grant_version,
                            "summary_authorized_use": summary_retention_grant.authorized_use,
                        }
                        if provider_summary is not None
                        and summary_retention_grant is not None
                        else {}
                    ),
                )
            )
        articles.sort(key=lambda item: (item.published_at, item.provider_article_id))
        warnings = [
            ProviderWarning(
                code=(
                    "private_provider_summaries_retained"
                    if summary_retention_grant is not None
                    else "metadata_only_news"
                ),
                message=(
                    "Massive descriptions and ticker-specific sentiment reasoning are "
                    "retained only in private evidence under the supplied versioned grant; "
                    "they are provider summaries, not article bodies."
                    if summary_retention_grant is not None
                    else "Massive descriptions and sentiment reasoning are not retained; "
                    "only rights-safe metadata and ticker sentiment labels are exposed."
                ),
            )
        ]
        for code, count in (
            ("malformed_news_rows", malformed),
            ("future_news_rows", future),
            ("duplicate_news_rows", duplicates),
            ("unknown_sentiment_labels", unknown_sentiment),
        ):
            if count:
                warnings.append(
                    ProviderWarning(
                        code=code, message=f"Excluded {count} Massive news row(s)."
                    )
                )
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

    def get_daily_ticker_summary(
        self,
        ticker: str,
        trading_date: date,
        *,
        adjusted: bool = False,
    ) -> ProviderResponse[DailyTickerSummary]:
        """Return a historical daily summary, including provider-native AH close.

        Event-reaction calculations require ``adjusted=False`` so the regular
        close and extended-hours prices remain on the same traded-price basis.
        """

        symbol = normalized_ticker(ticker)
        if adjusted:
            raise ValueError("historical after-hours reaction requires adjusted=False")
        fetch = self._http.get_json(
            provider=self.provider_name,
            url=f"{self.daily_summary_url}/{symbol}/{trading_date.isoformat()}",
            parameters={"adjusted": "false"},
            headers={"Authorization": f"Bearer {self._api_key}"},
            daily_budget=self._daily_budget,
            max_age_seconds=None if trading_date < date.today() else 60 * 60,
            minimum_interval_seconds=self.minimum_interval_seconds,
            payload_validator=lambda payload: _validate_daily_summary(
                payload, symbol=symbol, trading_date=trading_date
            ),
        )
        payload = require_mapping(fetch.payload, "Massive daily summary")
        summary = DailyTickerSummary(
            ticker=symbol,
            trading_date=trading_date,
            open=decimal_or_none(payload.get("open")),
            high=decimal_or_none(payload.get("high")),
            low=decimal_or_none(payload.get("low")),
            close=decimal_or_none(payload.get("close")),
            volume=decimal_or_none(payload.get("volume")),
            pre_market=decimal_or_none(payload.get("preMarket")),
            after_hours=decimal_or_none(payload.get("afterHours")),
            adjusted=False,
        )
        warnings = ()
        quality = DataQualityStatus.COMPLETE
        if summary.after_hours is None:
            quality = DataQualityStatus.PARTIAL
            warnings = (
                ProviderWarning(
                    code="after_hours_price_missing",
                    message="Massive daily summary omitted the after-hours close.",
                ),
            )
        return ProviderResponse(
            data=summary,
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=quality,
                coverage_start=trading_date,
                coverage_end=trading_date,
                warnings=warnings,
            ),
        )


def _validate_payload(value: Any) -> None:
    payload = require_mapping(value, "Massive")
    if payload.get("status") not in {None, "OK"}:
        raise ProviderPayloadError("Massive returned an unsuccessful status")
    if not isinstance(payload.get("results"), list):
        raise ProviderPayloadError("Massive news response omitted results")


def _validate_daily_summary(value: Any, *, symbol: str, trading_date: date) -> None:
    payload = require_mapping(value, "Massive daily summary")
    if payload.get("status") not in {None, "OK"}:
        raise ProviderPayloadError(
            "Massive returned an unsuccessful daily-summary status"
        )
    if str(payload.get("symbol", "")).upper() != symbol:
        raise ProviderPayloadError("Massive daily summary returned the wrong ticker")
    if str(payload.get("from", "")) != trading_date.isoformat():
        raise ProviderPayloadError("Massive daily summary returned the wrong date")
    for field in ("open", "high", "low", "close", "volume"):
        if decimal_or_none(payload.get(field)) is None:
            raise ProviderPayloadError(
                f"Massive daily summary omitted required field {field}"
            )


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _absolute_url(value: Any) -> str | None:
    text = _text(value)
    parsed = urlparse(text or "")
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "unknown").removeprefix("www.")


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _tickers(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        sorted({str(item).strip().upper() for item in value if str(item).strip()})
    )


def _ticker_insight(value: Any, symbol: str) -> tuple[str | None, str | None]:
    if not isinstance(value, list):
        return None, None
    for insight in value:
        if (
            isinstance(insight, dict)
            and str(insight.get("ticker", "")).upper() == symbol
        ):
            sentiment = _text(insight.get("sentiment"))
            return (
                sentiment.casefold() if sentiment else None,
                _text(insight.get("sentiment_reasoning")),
            )
    return None, None


def _provider_summary(
    *,
    description: str | None,
    sentiment_reasoning: str | None,
    grant: MassiveSummaryRetentionGrant | None,
) -> str | None:
    if grant is None:
        return None
    sections = tuple(
        section
        for section in (
            f"Article description: {description}" if description else None,
            (
                f"Ticker sentiment reasoning: {sentiment_reasoning}"
                if sentiment_reasoning
                else None
            ),
        )
        if section is not None
    )
    if not sections:
        return None
    combined = "\n\n".join(sections)
    # Never truncate provider text and then present it as a direct pull.  An
    # over-bound value stays withheld instead.
    return combined if len(combined) <= grant.max_summary_characters else None


def _ticker_match(symbol: str, related: tuple[str, ...]) -> NewsTickerMatch:
    if related == (symbol,):
        return NewsTickerMatch.EXACT
    if symbol in related:
        return NewsTickerMatch.RELATED
    return NewsTickerMatch.MISMATCH if related else NewsTickerMatch.UNKNOWN


__all__ = ["MassiveProvider", "MassiveSummaryRetentionGrant"]
