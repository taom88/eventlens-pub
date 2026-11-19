"""Normalized provider payloads with mandatory provenance."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.domain.events import EventTiming


class ProviderOrigin(StrEnum):
    LIVE = "live"
    CACHE = "cache"
    FIXTURE = "fixture"
    LOCAL_FILE = "local_file"


class DataQualityStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ProviderWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ProviderProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    origin: ProviderOrigin
    retrieved_at: datetime
    request_parameters: dict[str, str] = Field(default_factory=dict)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage_start: date | None = None
    coverage_end: date | None = None
    quota_consumed: bool
    data_quality: DataQualityStatus
    warnings: tuple[ProviderWarning, ...] = ()

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(timezone.utc)


PayloadT = TypeVar("PayloadT", bound=BaseModel)


class ProviderResponse(BaseModel, Generic[PayloadT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance: ProviderProvenance
    data: PayloadT


class DailyBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trading_date: date
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    adjusted_close: Decimal | None = Field(default=None, gt=0)
    volume: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_ohlc_range(self) -> "DailyBar":
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("daily high/low must contain open and close")
        if self.high < self.low:
            raise ValueError("daily high must be greater than or equal to low")
        return self


class MarketHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    currency: str = "USD"
    exchange_timezone: str = "America/New_York"
    bars: tuple[DailyBar, ...]

    @model_validator(mode="after")
    def bars_are_strictly_ordered(self) -> "MarketHistory":
        if any(
            left.trading_date >= right.trading_date
            for left, right in zip(self.bars, self.bars[1:])
        ):
            raise ValueError("market-history bars must be strictly ordered by trading date")
        return self


class DailyTickerSummary(BaseModel):
    """One provider-native daily session with optional extended-hours closes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    trading_date: date
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    pre_market: Decimal | None = Field(default=None, gt=0)
    after_hours: Decimal | None = Field(default=None, gt=0)
    adjusted: bool

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_ohlc_range(self) -> DailyTickerSummary:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("daily high/low must contain open and close")
        if self.high < self.low:
            raise ValueError("daily high must be greater than or equal to low")
        return self


class FundamentalObservation(BaseModel):
    """One point-in-time fundamental value or an explicit missing value."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    metric_key: str = Field(min_length=1)
    value_numeric: Decimal | None = None
    unit: str = Field(min_length=1)
    fiscal_period: str = Field(min_length=1)
    period_end: date | None = None
    accounting_basis: str = Field(min_length=1)
    available_at: datetime
    source_ref: str = Field(min_length=1)
    missing_reason: str | None = None

    @field_validator("metric_key", "fiscal_period", "unit", "accounting_basis", "source_ref")
    @classmethod
    def normalize_labels(cls, value: str, info) -> str:
        normalized = value.strip()
        if info.field_name == "fiscal_period":
            normalized = normalized.upper()
        return normalized

    @field_validator("available_at")
    @classmethod
    def available_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fundamental available_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def value_and_missing_reason_are_exclusive(self) -> "FundamentalObservation":
        if self.value_numeric is None and not (self.missing_reason or "").strip():
            raise ValueError("missing fundamental values require missing_reason")
        if self.value_numeric is not None and self.missing_reason is not None:
            raise ValueError("known fundamental values cannot carry missing_reason")
        return self


class FundamentalSnapshot(BaseModel):
    """Cutoff-safe fundamental observations with provider provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    as_of_at: datetime
    observations: tuple[FundamentalObservation, ...]

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("as_of_at")
    @classmethod
    def as_of_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fundamental snapshot as_of_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def observations_are_cutoff_safe_and_unique(self) -> "FundamentalSnapshot":
        if any(item.available_at > self.as_of_at for item in self.observations):
            raise ValueError("fundamental observation is available after the snapshot cutoff")
        keys = [(item.metric_key, item.fiscal_period) for item in self.observations]
        if len(keys) != len(set(keys)):
            raise ValueError("fundamental metric/period observations must be unique")
        return self


class EarningsEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    event_date: date
    event_release_at: datetime | None = None
    fiscal_quarter: str | None = None
    fiscal_period_end: date | None = None
    timing: EventTiming = EventTiming.UNKNOWN
    source_event_id: str = Field(min_length=1)
    reported_eps: Decimal | None = None
    estimated_eps: Decimal | None = None

    @field_validator("event_release_at")
    @classmethod
    def release_at_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_release_at must be timezone-aware")
        return value.astimezone(timezone.utc)

class EarningsEvents(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    events: tuple[EarningsEventRecord, ...]


class FilingEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    filing_date: date
    form: str = Field(min_length=1)
    items: tuple[str, ...]
    accession_number: str = Field(min_length=1)


class FilingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    filings: tuple[FilingEvidenceRecord, ...]


class TranscriptPassage(BaseModel):
    """One provider-native transcript passage.

    ``speaker`` and ``role`` deliberately retain the provider's raw labels.
    The context-card transcript parser adds normalized roles and Q&A structure
    without replacing these addressable source fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    passage_id: str = Field(min_length=1)
    speaker: str | None = None
    role: str | None = None
    section: str | None = None
    company: str | None = None
    firm: str | None = None
    text: str = Field(min_length=1)


class TranscriptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    fiscal_quarter: str
    event_date: date | None = None
    passages: tuple[TranscriptPassage, ...]


class NewsTickerMatch(StrEnum):
    EXACT = "exact"
    RELATED = "related"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class NewsCutoffStatus(StrEnum):
    ELIGIBLE_BEFORE_CUTOFF = "eligible_before_cutoff"


class NewsSummaryStatus(StrEnum):
    RETAINED_PERMITTED = "retained_permitted"
    WITHHELD_NO_PERMISSION = "withheld_no_permission"
    MISSING_FROM_PROVIDER = "missing_from_provider"


class NewsArticleMetadata(BaseModel):
    """Discovery metadata; ``provider_summary`` is never an article body."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    ticker: str = Field(min_length=1)
    provider_article_id: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    provider_summary: str | None = Field(default=None, max_length=10_000)
    summary_status: NewsSummaryStatus = NewsSummaryStatus.MISSING_FROM_PROVIDER
    source_name: str | None = None
    source_domain: str | None = None
    url: str = Field(min_length=1)
    published_at: datetime
    retrieved_at: datetime | None = None
    category: str | None = None
    related_tickers: tuple[str, ...] = ()
    ticker_match: NewsTickerMatch = NewsTickerMatch.UNKNOWN
    provider_fields: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    cutoff_status: NewsCutoffStatus = NewsCutoffStatus.ELIGIBLE_BEFORE_CUTOFF
    overall_sentiment_score: Decimal | None = None
    overall_sentiment_label: str | None = None
    ticker_relevance_score: Decimal | None = None
    ticker_sentiment_score: Decimal | None = None
    ticker_sentiment_label: str | None = None
    discovery_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    item_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("related_tickers", mode="before")
    @classmethod
    def normalize_related_tickers(cls, value: object) -> object:
        if value is None:
            return ()
        return tuple(item.strip().upper() for item in value if str(item).strip())  # type: ignore[union-attr]

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("news timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def url_and_domain_are_metadata_only(self) -> "NewsArticleMetadata":
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP(S) URL")
        if self.source_domain is not None and "://" in self.source_domain:
            raise ValueError("source_domain must be a domain label, not a URL")
        if len(set(self.related_tickers)) != len(self.related_tickers):
            raise ValueError("related_tickers cannot contain duplicates")
        if (self.provider_summary is None) != (self.summary_status is not NewsSummaryStatus.RETAINED_PERMITTED):
            raise ValueError("provider_summary may exist only when retention is explicitly permitted")
        expected_discovery = self.canonical_discovery_fingerprint()
        if self.discovery_fingerprint is None:
            object.__setattr__(self, "discovery_fingerprint", expected_discovery)
        elif self.discovery_fingerprint != expected_discovery:
            raise ValueError("discovery_fingerprint does not match normalized discovery metadata")
        expected = self.canonical_sha256()
        if self.item_sha256 is None:
            object.__setattr__(self, "item_sha256", expected)
        elif self.item_sha256 != expected:
            raise ValueError("item_sha256 does not match canonical news metadata")
        return self

    def canonical_discovery_fingerprint(self) -> str:
        """Stable across retrievals of the same normalized provider record."""

        canonical = self.model_dump(
            mode="json",
            exclude={"retrieved_at", "discovery_fingerprint", "item_sha256"},
        )
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def canonical_sha256(self) -> str:
        """Return the deterministic hash for this metadata row.

        Keeping this calculation on the normalized model lets downstream
        receipt assembly detect a stale hash after an otherwise-valid model
        copy/update operation.
        """

        canonical = self.model_dump(mode="json", exclude={"item_sha256"})
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class CompanyNewsMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    articles: tuple[NewsArticleMetadata, ...]

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def articles_match_ticker_and_order(self) -> "CompanyNewsMetadata":
        if any(article.ticker != self.ticker for article in self.articles):
            raise ValueError("company-news article tickers must match the response ticker")
        if any(
            left.published_at > right.published_at
            for left, right in zip(self.articles, self.articles[1:])
        ):
            raise ValueError("company-news articles must be ordered by published_at")
        return self
