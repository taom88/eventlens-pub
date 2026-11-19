"""No-key yfinance adapter used as the default US-equity OHLCV source."""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ._common import normalized_ticker, provenance
from .errors import ProviderConfigurationError, ProviderPayloadError
from .http import HttpFetch
from .models import (
    DailyBar,
    DataQualityStatus,
    MarketHistory,
    ProviderOrigin,
    ProviderResponse,
    ProviderWarning,
)


HistoryLoader = Callable[[str, date, date], Iterable[Mapping[str, Any]]]


class YFinanceProvider:
    provider_name = "yfinance"

    def __init__(self, history_loader: HistoryLoader | None = None) -> None:
        self._history_loader = history_loader or _load_yfinance_history
        try:
            self.provider_version = version("yfinance")
        except PackageNotFoundError:
            self.provider_version = "not-installed"

    def get_daily_history(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[MarketHistory]:
        symbol = normalized_ticker(ticker)
        if end < start:
            raise ValueError("end must be on or after start")
        rows = list(self._history_loader(symbol, start, end))
        bars = tuple(sorted((
            DailyBar(
                trading_date=_coerce_date(row["date"]),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                adjusted_close=Decimal(str(row["adjusted_close"])),
                volume=int(row["volume"]),
            )
            for row in rows
            if start <= _coerce_date(row["date"]) <= end
        ), key=lambda bar: bar.trading_date))
        if not bars:
            raise ProviderPayloadError("yfinance returned no daily bars for the requested window")
        canonical = json.dumps(
            [bar.model_dump(mode="json") for bar in bars],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        parameters = {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat()}
        fingerprint = hashlib.sha256(
            json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        fetch = HttpFetch(
            payload={},
            raw_bytes=canonical,
            origin=ProviderOrigin.LIVE,
            retrieved_at=datetime.now(timezone.utc),
            safe_request_parameters=parameters,
            request_fingerprint=fingerprint,
            quota_consumed=False,
        )
        return ProviderResponse(
            data=MarketHistory(ticker=symbol, bars=bars),
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=DataQualityStatus.COMPLETE,
                coverage_start=bars[0].trading_date,
                coverage_end=bars[-1].trading_date,
                warnings=(
                    ProviderWarning(
                        code="normalized_adapter_checksum",
                        message=(
                            "The provenance checksum covers normalized yfinance rows because "
                            "the library does not expose the raw upstream HTTP response."
                        ),
                    ),
                ),
            ),
        )


def _load_yfinance_history(symbol: str, start: date, end: date) -> Iterable[Mapping[str, Any]]:
    try:
        import yfinance as yf
    except ImportError as error:
        raise ProviderConfigurationError(
            "yfinance is optional; install EventLens with pip install -e '.[free-data]'"
        ) from error
    frame = yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        actions=False,
    )
    for index, row in frame.iterrows():
        adjusted_close = row.get("Adj Close")
        if adjusted_close is None or str(adjusted_close).casefold() == "nan":
            raise ProviderPayloadError(
                "yfinance omitted adjusted close; canonical volatility calculations are blocked"
            )
        yield {
            "date": index.date(),
            "open": row["Open"],
            "high": row["High"],
            "low": row["Low"],
            "close": row["Close"],
            "adjusted_close": adjusted_close,
            "volume": row["Volume"],
        }


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
