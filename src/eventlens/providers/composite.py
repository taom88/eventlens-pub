"""Quota-efficient provider compositions for the live workflow."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from eventlens.domain.events import EventTiming

from .errors import ProviderEntitlementError, ProviderPayloadError
from .http import ProviderHTTPError, ProviderQuotaExceededError
from .models import (
    CompanyNewsMetadata,
    DataQualityStatus,
    EarningsEvents,
    ProviderOrigin,
    ProviderProvenance,
    ProviderResponse,
    ProviderWarning,
)
from .protocols import EventMetadataProvider, NewsProvider


class FreeFirstEventMetadataProvider:
    """Merge cached long history from Alpha Vantage with one Finnhub near window."""

    provider_name = "free_first_event_composite"
    provider_version = "1.0"

    def __init__(
        self,
        *,
        historical: EventMetadataProvider,
        near_term: EventMetadataProvider,
    ) -> None:
        self._historical = historical
        self._near_term = near_term

    def get_earnings_events(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[EarningsEvents]:
        historical = self._historical.get_earnings_events(ticker, start, end)
        near_end = end
        near_start = max(start, near_end - timedelta(days=31))
        near = self._near_term.get_earnings_events(ticker, near_start, near_end)

        # Finnhub's timing-aware record wins only for its narrow date window;
        # Alpha Vantage supplies the long, cacheable historical dates.
        by_date = {item.event_date: item for item in historical.data.events}
        by_date.update({item.event_date: item for item in near.data.events})
        events = tuple(by_date[key] for key in sorted(by_date))
        children = (historical.provenance, near.provenance)
        fingerprint = hashlib.sha256(
            "|".join(item.request_fingerprint for item in children).encode()
        ).hexdigest()
        raw_checksum = hashlib.sha256(
            "|".join(item.raw_response_sha256 for item in children).encode()
        ).hexdigest()
        warnings = tuple(
            warning for item in children for warning in item.warnings
        ) + (
            ProviderWarning(
                code="composite_event_routing",
                message=(
                    "Alpha Vantage supplied long earnings history; Finnhub supplied "
                    "the final <=31-day timing-aware window."
                ),
            ),
        )
        origin = (
            ProviderOrigin.LIVE
            if any(item.origin is ProviderOrigin.LIVE for item in children)
            else ProviderOrigin.CACHE
        )
        quality = (
            DataQualityStatus.COMPLETE
            if all(
                item.data_quality is DataQualityStatus.COMPLETE
                for item in children
            )
            and all(item.timing is not EventTiming.UNKNOWN for item in events)
            else DataQualityStatus.PARTIAL
        )
        return ProviderResponse(
            data=EarningsEvents(ticker=ticker.upper(), events=events),
            provenance=ProviderProvenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                origin=origin,
                retrieved_at=max(item.retrieved_at for item in children),
                request_parameters={
                    "ticker": ticker.upper(),
                    "history_start": start.isoformat(),
                    "history_end": end.isoformat(),
                    "finnhub_window_start": near_start.isoformat(),
                    "finnhub_window_end": near_end.isoformat(),
                },
                request_fingerprint=fingerprint,
                raw_response_sha256=raw_checksum,
                coverage_start=events[0].event_date if events else None,
                coverage_end=events[-1].event_date if events else None,
                quota_consumed=any(item.quota_consumed for item in children),
                data_quality=quality,
                warnings=warnings,
            ),
        )


class FreeFirstNewsProvider:
    """Use Alpha Vantage news first, with an auditable Finnhub fallback."""

    provider_name = "free_first_news_composite"
    provider_version = "1.0"

    def __init__(self, *, primary: NewsProvider, fallback: NewsProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    def get_company_news(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        cutoff_at: datetime | None = None,
    ) -> ProviderResponse[CompanyNewsMetadata]:
        try:
            primary = self._get_news(self._primary, ticker, start, end, cutoff_at=cutoff_at)
            if primary.data.articles:
                return primary
            reason = "primary_empty"
        except (
            ProviderEntitlementError,
            ProviderPayloadError,
            ProviderQuotaExceededError,
            ProviderHTTPError,
        ) as error:
            reason = f"primary_unavailable:{type(error).__name__}"
        fallback = self._get_news(self._fallback, ticker, start, end, cutoff_at=cutoff_at)
        provenance = fallback.provenance.model_copy(
            update={
                "warnings": fallback.provenance.warnings
                + (ProviderWarning(code="news_fallback", message=reason),),
            }
        )
        return ProviderResponse(data=fallback.data, provenance=provenance)

    @staticmethod
    def _get_news(
        provider: NewsProvider,
        ticker: str,
        start: date,
        end: date,
        *,
        cutoff_at: datetime | None,
    ) -> ProviderResponse[CompanyNewsMetadata]:
        # Keep the no-cutoff call compatible with small test doubles and older
        # providers while preserving an explicitly supplied historical cutoff.
        if cutoff_at is None:
            return provider.get_company_news(ticker, start, end)
        return provider.get_company_news(ticker, start, end, cutoff_at=cutoff_at)
