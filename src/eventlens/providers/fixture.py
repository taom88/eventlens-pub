"""Deterministic offline provider backed by versioned JSON fixture bundles."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from eventlens.domain.volatility import OptionChainSnapshot

from ._common import normalized_ticker
from .errors import ProviderPayloadError
from .models import (
    CompanyNewsMetadata,
    DataQualityStatus,
    EarningsEvents,
    FundamentalObservation,
    FundamentalSnapshot,
    MarketHistory,
    ProviderOrigin,
    ProviderProvenance,
    ProviderResponse,
    ProviderWarning,
    TranscriptRecord,
)


class FixtureProvider:
    provider_name = "fixture"
    provider_version = "fixture-v1"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def get_daily_history(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[MarketHistory]:
        self._validate_date_range(start, end)
        symbol, document, raw, retrieved_at = self._load(ticker)
        data = MarketHistory.model_validate(document.get("market_history"))
        self._validate_ticker(data.ticker, symbol, "market_history.ticker")
        bars = tuple(bar for bar in data.bars if start <= bar.trading_date <= end)
        coverage_start, coverage_end, quality, warnings = self._window_coverage(
            (bar.trading_date for bar in bars), start, end, "market-history bars"
        )
        return self._response(
            symbol,
            MarketHistory(ticker=symbol, currency=data.currency, exchange_timezone=data.exchange_timezone, bars=bars),
            raw,
            "market_history",
            start,
            end,
            retrieved_at,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            data_quality=quality,
            warnings=warnings,
        )

    def get_fundamentals(
        self, ticker: str, as_of_at: datetime, *, fiscal_period: str | None = None
    ) -> ProviderResponse[FundamentalSnapshot]:
        if as_of_at.tzinfo is None or as_of_at.utcoffset() is None:
            raise ValueError("as_of_at must be timezone-aware")
        symbol, document, raw, retrieved_at = self._load(ticker)
        normalized_as_of = as_of_at.astimezone(timezone.utc)
        rows = tuple(
            FundamentalObservation.model_validate(item)
            for item in document.get("fundamentals", [])
        )
        rows = tuple(
            item for item in rows
            if item.available_at <= normalized_as_of
            and (fiscal_period is None or item.fiscal_period == fiscal_period.strip().upper())
        )
        rows = tuple(sorted(rows, key=lambda item: (item.fiscal_period, item.metric_key)))
        quality = DataQualityStatus.COMPLETE if rows else DataQualityStatus.UNAVAILABLE
        warnings = () if rows else (
            ProviderWarning(
                code="fundamentals_unavailable_at_cutoff",
                message="Fixture contains no fundamentals available at the requested cutoff.",
            ),
        )
        return self._response(
            symbol,
            FundamentalSnapshot(ticker=symbol, as_of_at=normalized_as_of, observations=rows),
            raw,
            "fundamentals",
            retrieved_at=retrieved_at,
            data_quality=quality,
            warnings=warnings,
            extra_parameters={
                "as_of_at": normalized_as_of.isoformat(),
                "fiscal_period": fiscal_period.strip().upper() if fiscal_period else "",
            },
        )

    def get_earnings_events(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[EarningsEvents]:
        self._validate_date_range(start, end)
        symbol, document, raw, retrieved_at = self._load(ticker)
        data = EarningsEvents.model_validate(document.get("earnings_events"))
        self._validate_ticker(data.ticker, symbol, "earnings_events.ticker")
        for item in data.events:
            self._validate_ticker(
                item.ticker, symbol, f"earnings_events.events[{item.source_event_id}].ticker"
            )
        events = tuple(item for item in data.events if start <= item.event_date <= end)
        coverage_start, coverage_end, quality, warnings = self._window_coverage(
            (item.event_date for item in events), start, end, "earnings events"
        )
        return self._response(
            symbol,
            EarningsEvents(ticker=symbol, events=events),
            raw,
            "earnings_events",
            start,
            end,
            retrieved_at,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            data_quality=quality,
            warnings=warnings,
        )

    def get_transcript(
        self, ticker: str, fiscal_quarter: str
    ) -> ProviderResponse[TranscriptRecord]:
        symbol, document, raw, retrieved_at = self._load(ticker)
        quarter = self._normalize_fiscal_quarter(fiscal_quarter)
        transcripts = document.get("transcripts")
        if not isinstance(transcripts, dict) or quarter not in transcripts:
            raise ProviderPayloadError(f"fixture omitted transcript {quarter}")
        data = TranscriptRecord.model_validate(transcripts[quarter])
        self._validate_ticker(data.ticker, symbol, f"transcripts[{quarter}].ticker")
        if data.fiscal_quarter != quarter:
            raise ProviderPayloadError(
                f"transcripts[{quarter}].fiscal_quarter must equal its fixture key"
            )
        coverage_start = data.event_date
        quality = (
            DataQualityStatus.COMPLETE
            if coverage_start is not None
            else DataQualityStatus.PARTIAL
        )
        warnings = ()
        if coverage_start is None:
            warnings = (
                ProviderWarning(
                    code="transcript_event_date_missing",
                    message="Fixture transcript has no event date; date coverage is partial.",
                ),
            )
        return self._response(
            symbol,
            data,
            raw,
            f"transcript:{quarter}",
            retrieved_at=retrieved_at,
            coverage_start=coverage_start,
            coverage_end=coverage_start,
            data_quality=quality,
            warnings=warnings,
        )

    def get_company_news(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[CompanyNewsMetadata]:
        self._validate_date_range(start, end)
        symbol, document, raw, retrieved_at = self._load(ticker)
        data = CompanyNewsMetadata.model_validate(
            document.get("company_news", {"ticker": symbol, "articles": []})
        )
        self._validate_ticker(data.ticker, symbol, "company_news.ticker")
        articles = tuple(
            article
            for article in data.articles
            if start <= article.published_at.date() <= end
        )
        coverage_start, coverage_end, quality, warnings = self._window_coverage(
            (article.published_at.date() for article in articles),
            start,
            end,
            "company-news metadata records",
        )
        return self._response(
            symbol,
            CompanyNewsMetadata(ticker=symbol, articles=articles),
            raw,
            "company_news",
            start,
            end,
            retrieved_at,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            data_quality=quality,
            warnings=warnings,
        )

    def get_historical_options(
        self, ticker: str, snapshot_date: date
    ) -> ProviderResponse[OptionChainSnapshot]:
        symbol, document, raw, retrieved_at = self._load(ticker)
        options = document.get("option_snapshots")
        key = snapshot_date.isoformat()
        if not isinstance(options, dict) or key not in options:
            raise ProviderPayloadError(f"fixture omitted option snapshot {key}")
        data = OptionChainSnapshot.model_validate(options[key])
        self._validate_ticker(data.ticker, symbol, f"option_snapshots[{key}].ticker")
        if data.snapshot_date != snapshot_date:
            raise ProviderPayloadError(
                f"option_snapshots[{key}].snapshot_date must equal the requested snapshot date"
            )
        return self._response(
            symbol,
            data,
            raw,
            f"options:{key}",
            snapshot_date,
            snapshot_date,
            retrieved_at,
            coverage_start=data.snapshot_date,
            coverage_end=data.snapshot_date,
            data_quality=DataQualityStatus.COMPLETE,
        )

    def _load(self, ticker: str) -> tuple[str, dict[str, Any], bytes, datetime]:
        symbol = normalized_ticker(ticker)
        path = (self._root / f"{symbol}.json").resolve()
        if path.parent != self._root:
            raise ProviderPayloadError("fixture path escaped configured root")
        try:
            raw = path.read_bytes()
            document = json.loads(raw)
        except (OSError, json.JSONDecodeError) as error:
            raise ProviderPayloadError(f"fixture is unavailable for {symbol}") from error
        if not isinstance(document, dict) or document.get("schema_version") != "1.0":
            raise ProviderPayloadError("fixture schema_version must be 1.0")
        bundle_ticker = document.get("ticker")
        if not isinstance(bundle_ticker, str):
            raise ProviderPayloadError("fixture ticker must be a non-empty string")
        self._validate_ticker(bundle_ticker, symbol, "fixture.ticker")
        try:
            retrieved_at = datetime.fromisoformat(str(document["retrieved_at"]))
        except (KeyError, ValueError) as error:
            raise ProviderPayloadError(
                "fixture retrieved_at must be a timezone-aware ISO timestamp"
            ) from error
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ProviderPayloadError(
                "fixture retrieved_at must be a timezone-aware ISO timestamp"
            )
        return symbol, document, raw, retrieved_at.astimezone(timezone.utc)

    def _response(
        self,
        ticker: str,
        data: Any,
        raw: bytes,
        section: str,
        start: date | None = None,
        end: date | None = None,
        retrieved_at: datetime | None = None,
        coverage_start: date | None = None,
        coverage_end: date | None = None,
        data_quality: DataQualityStatus = DataQualityStatus.COMPLETE,
        warnings: tuple[ProviderWarning, ...] = (),
        extra_parameters: dict[str, str] | None = None,
    ) -> ProviderResponse[Any]:
        parameters = {"ticker": ticker, "section": section}
        if start is not None:
            parameters["start"] = start.isoformat()
        if end is not None:
            parameters["end"] = end.isoformat()
        if extra_parameters:
            parameters.update(extra_parameters)
        fingerprint = hashlib.sha256(
            json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ProviderResponse(
            data=data,
            provenance=ProviderProvenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                origin=ProviderOrigin.FIXTURE,
                retrieved_at=retrieved_at or datetime.now(timezone.utc),
                request_parameters=parameters,
                request_fingerprint=fingerprint,
                raw_response_sha256=hashlib.sha256(raw).hexdigest(),
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                quota_consumed=False,
                data_quality=data_quality,
                warnings=warnings,
            ),
        )

    @staticmethod
    def _validate_date_range(start: date, end: date) -> None:
        if start > end:
            raise ValueError("start must be on or before end")

    @staticmethod
    def _normalize_fiscal_quarter(value: str) -> str:
        quarter = value.strip().upper()
        if not quarter:
            raise ValueError("fiscal_quarter must not be blank")
        return quarter

    @staticmethod
    def _window_coverage(
        observed_dates: Any, start: date, end: date, description: str
    ) -> tuple[date | None, date | None, DataQualityStatus, tuple[ProviderWarning, ...]]:
        dates = tuple(observed_dates)
        if not dates:
            return (
                None,
                None,
                DataQualityStatus.UNAVAILABLE,
                (
                    ProviderWarning(
                        code="requested_window_unavailable",
                        message=f"Fixture contains no {description} in the requested window.",
                    ),
                ),
            )
        coverage_start = min(dates)
        coverage_end = max(dates)
        if coverage_start == start and coverage_end == end:
            return coverage_start, coverage_end, DataQualityStatus.COMPLETE, ()
        return (
            coverage_start,
            coverage_end,
            DataQualityStatus.PARTIAL,
            (
                ProviderWarning(
                    code="requested_window_partial",
                    message=(
                        f"Fixture {description} cover {coverage_start.isoformat()} through "
                        f"{coverage_end.isoformat()}, not the full requested window "
                        f"{start.isoformat()} through {end.isoformat()}."
                    ),
                ),
            ),
        )

    @staticmethod
    def _validate_ticker(value: str, expected: str, location: str) -> None:
        try:
            actual = normalized_ticker(value)
        except (AttributeError, ValueError) as error:
            raise ProviderPayloadError(f"{location} must contain a valid ticker") from error
        if actual != expected:
            raise ProviderPayloadError(
                f"{location} ticker {actual} does not match requested ticker {expected}"
            )
