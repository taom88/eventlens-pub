"""Deterministic preflight contract for an upcoming live issuer event."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.config import EventLensSettings
from eventlens.providers.composite import FreeFirstNewsProvider
from eventlens.providers.errors import ProviderConfigurationError, ProviderError
from eventlens.providers.http import ProviderHTTPError, ProviderQuotaExceededError
from eventlens.providers.models import EarningsEvents, ProviderResponse
from eventlens.providers.registry import FreeFirstProviderRegistry, build_free_first_registry


class ReleaseTimeBasis(StrEnum):
    SEC_VERIFIED = "sec_verified"
    ISSUER_CONFIRMED = "issuer_confirmed"
    ASSUMED_AFTER_MARKET_CLOSE = "assumed_after_market_close"


class UpcomingLivePreflight(BaseModel):
    """Request artifact contract; no provider secrets or article text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    event_date: date
    event_release_at: datetime
    release_time_basis: ReleaseTimeBasis
    timing_warning: str | None = None
    prior_fiscal_quarter: str = Field(min_length=1)
    post_event_trading_dates: tuple[date, ...]
    session_id: str = Field(default_factory=lambda: f"live-{uuid4().hex}")
    idempotency_key: str = Field(default_factory=lambda: uuid4().hex)
    approval_required: bool = True
    live_provider_run: bool = True

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("event_release_at")
    @classmethod
    def release_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_release_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_contract(self) -> "UpcomingLivePreflight":
        if len(self.post_event_trading_dates) != 5:
            raise ValueError("exactly five post-event trading dates are required")
        if len(set(self.post_event_trading_dates)) != 5:
            raise ValueError("post-event trading dates must be unique")
        if any(day <= self.event_date for day in self.post_event_trading_dates):
            raise ValueError("post-event trading dates must follow event_date")
        if self.release_time_basis is ReleaseTimeBasis.ASSUMED_AFTER_MARKET_CLOSE and not self.timing_warning:
            raise ValueError("assumed timing requires an explicit warning")
        return self


__all__ = ["ReleaseTimeBasis", "UpcomingLivePreflight"]


def write_preflight_artifact(payload: dict, output: Path) -> tuple[Path, Path, str]:
    """Write canonical preview JSON and its SHA atomically."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(canonical, encoding="utf-8")
    temp.replace(output)
    sha_path = output.with_suffix(output.suffix + ".sha256")
    sha_temp = sha_path.with_suffix(sha_path.suffix + ".tmp")
    sha_temp.write_text(digest + "\n", encoding="utf-8")
    sha_temp.replace(sha_path)
    return output, sha_path, digest


def run_upcoming_preflight(
    settings: EventLensSettings,
    *,
    ticker: str,
    window_start: date,
    window_end: date,
    output: Path,
) -> dict:
    """Resolve one upcoming event and emit a preview; never writes memory."""
    if settings.data_mode != "live":
        raise ProviderConfigurationError(
            "live preflight requires EVENTLENS_DATA_MODE=live; fixture mode is refused"
        )
    registry = build_free_first_registry(settings)
    if registry.finnhub is None and registry.alpha_vantage is None:
        raise ProviderConfigurationError("configure Finnhub or Alpha Vantage before live preflight")
    event_provider = _event_provider(registry)
    ticker = ticker.strip().upper()
    events = event_provider.get_earnings_events(ticker, window_start, window_end)
    if len(events.data.events) != 1:
        raise ValueError(f"expected exactly one {ticker} event in window, found {len(events.data.events)}")
    event = events.data.events[0]
    release_at = event.event_release_at or datetime.combine(event.event_date, datetime.min.time(), tzinfo=timezone.utc).replace(hour=20)
    basis = ReleaseTimeBasis.ISSUER_CONFIRMED if event.event_release_at else ReleaseTimeBasis.ASSUMED_AFTER_MARKET_CLOSE
    warning = None if event.event_release_at else "Issuer release instant is unconfirmed; assumed 20:00 UTC after market close."
    news = None
    enrichment_errors: list[dict[str, str]] = []
    if registry.alpha_vantage is not None or registry.finnhub is not None:
        news_provider = FreeFirstNewsProvider(
            primary=registry.alpha_vantage or registry.finnhub, fallback=registry.finnhub or registry.alpha_vantage
        )
        cutoff = release_at - timedelta(seconds=1)
        try:
            news = news_provider.get_company_news(ticker, max(window_start, event.event_date - timedelta(days=31)), event.event_date, cutoff_at=cutoff)
        except (ProviderError, ProviderHTTPError, ProviderQuotaExceededError) as error:
            enrichment_errors.append({"component": "news", "error": type(error).__name__, "detail": str(error)})
    market_setup = None
    if registry.yfinance is not None:
        try:
            bars = registry.yfinance.get_daily_history(
                ticker, event.event_date - timedelta(days=30), event.event_date - timedelta(days=1)
            )
            market_setup = {
                "provider": bars.provenance.provider_name,
                "bar_count": len(bars.data.bars),
                "coverage_start": bars.provenance.coverage_start.isoformat() if bars.provenance.coverage_start else None,
                "coverage_end": bars.provenance.coverage_end.isoformat() if bars.provenance.coverage_end else None,
                "response_sha256": bars.provenance.raw_response_sha256,
                "status": "ready" if len(bars.data.bars) >= 5 else "insufficient_pre_event_history",
            }
        except (ProviderError, ProviderHTTPError, ProviderQuotaExceededError) as error:
            enrichment_errors.append({"component": "market", "error": type(error).__name__, "detail": str(error)})
            market_setup = {"status": "unavailable"}
    payload = {
        "status": "PREVIEW",
        "approval_state": "NOT_APPROVED",
        "memory_write": "FORBIDDEN",
        "ticker": ticker,
        "event_id": event.source_event_id,
        "event_date": event.event_date.isoformat(),
        "event_release_at": release_at.isoformat().replace("+00:00", "Z"),
        "release_time_basis": basis.value,
        "timing_warning": warning,
        "prior_fiscal_quarter": _prior_quarter(event.fiscal_quarter),
        "provider_event_receipt": events.provenance.model_dump(mode="json"),
        "news_receipt_summary": None if news is None else {
            "provider": news.provenance.provider_name,
            "response_sha256": news.provenance.raw_response_sha256,
            "article_count": len(news.data.articles),
            "item_hashes_sha256": hashlib.sha256(
                "|".join(item.item_sha256 or "" for item in news.data.articles).encode()
            ).hexdigest(),
            "cutoff_at": (release_at - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "rights_status": "provider_terms_review_pending",
        },
        "market_setup_summary": market_setup,
        "enrichment_errors": enrichment_errors,
        "config": {"data_mode": settings.data_mode, "memory_backend": settings.memory_backend},
    }
    _, _, digest = write_preflight_artifact(payload, output)
    payload["artifact_sha256"] = digest
    return payload


def _event_provider(registry: FreeFirstProviderRegistry):
    # Upcoming calendar resolution is a near-term task: prefer Finnhub's
    # timing-aware calendar so a degraded Alpha Vantage response cannot block
    # the preflight. Alpha Vantage remains the fallback when Finnhub is absent.
    if registry.finnhub is not None:
        return registry.finnhub
    if registry.alpha_vantage is not None:
        return registry.alpha_vantage
    return registry.alpha_vantage or registry.finnhub


def _prior_quarter(fiscal_quarter: str | None) -> str:
    if not fiscal_quarter:
        return "unknown"
    try:
        year_text, quarter_text = fiscal_quarter.upper().split("Q", 1)
        year, quarter = int(year_text), int(quarter_text)
        if quarter == 1:
            year, quarter = year - 1, 4
        else:
            quarter -= 1
        return f"{year}Q{quarter}"
    except (ValueError, TypeError):
        return "unknown"
