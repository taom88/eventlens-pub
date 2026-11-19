"""Replaceable provider interfaces used by application and graph layers."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from eventlens.domain.volatility import OptionChainSnapshot

from .models import (
    CompanyNewsMetadata,
    EarningsEvents,
    FilingEvidence,
    FundamentalSnapshot,
    MarketHistory,
    ProviderResponse,
    TranscriptRecord,
)


@runtime_checkable
class MarketDataProvider(Protocol):
    provider_name: str

    def get_daily_history(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[MarketHistory]: ...


@runtime_checkable
class EventMetadataProvider(Protocol):
    provider_name: str

    def get_earnings_events(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[EarningsEvents]: ...


@runtime_checkable
class TranscriptProvider(Protocol):
    provider_name: str

    def get_transcript(
        self, ticker: str, fiscal_quarter: str
    ) -> ProviderResponse[TranscriptRecord]: ...


@runtime_checkable
class OptionsDataProvider(Protocol):
    provider_name: str

    def get_historical_options(
        self, ticker: str, snapshot_date: date
    ) -> ProviderResponse[OptionChainSnapshot]: ...


@runtime_checkable
class FilingEvidenceProvider(Protocol):
    provider_name: str

    def get_earnings_filings(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[FilingEvidence]: ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    provider_name: str

    def get_fundamentals(
        self, ticker: str, as_of_at: datetime, *, fiscal_period: str | None = None
    ) -> ProviderResponse[FundamentalSnapshot]: ...


@runtime_checkable
class NewsProvider(Protocol):
    provider_name: str

    def get_company_news(
        self, ticker: str, start: date, end: date, *, cutoff_at: datetime | None = None
    ) -> ProviderResponse[CompanyNewsMetadata]: ...
