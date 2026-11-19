"""Provider orchestration for one authorization-gated live evidence collection."""

from __future__ import annotations

import hashlib
import html
import re
from calendar import month_name
from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from eventlens.domain.events import EventTiming
from eventlens.providers.errors import ProviderError
from eventlens.providers.http import ProviderHTTPError, ProviderQuotaExceededError
from eventlens.providers.models import (
    CompanyNewsMetadata,
    EarningsEventRecord,
    EarningsEvents,
    ProviderResponse,
)
from eventlens.providers.registry import FreeFirstProviderRegistry

from .live_evidence import (
    IssuerTimingRevalidation,
    IssuerTimingSource,
    LiveEvidenceContract,
    LiveEvidenceFailure,
    RequestBudgetReceipt,
    RequestBudgetUse,
    RetainedLiveEvidence,
)

_MAX_ISSUER_PAGE_BYTES = 5_000_000


def revalidate_issuer_timing(
    contract: LiveEvidenceContract,
    *,
    client: httpx.Client | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IssuerTimingRevalidation:
    """Reconfirm the frozen issuer date/timing without retaining page bodies."""

    checked_at = contract.require_collection_authorization(clock())
    owned_client = client is None
    requester = client or httpx.Client(timeout=30.0, follow_redirects=True)
    sources: list[IssuerTimingSource] = []
    pages: list[str] = []
    try:
        for url in contract.issuer_timing_urls:
            response = requester.get(
                url,
                headers={"User-Agent": "EventLens live acceptance revalidation"},
            )
            response.raise_for_status()
            raw = response.content
            if not raw or len(raw) > _MAX_ISSUER_PAGE_BYTES:
                raise ValueError("issuer timing page size is outside the accepted range")
            pages.append(_normalized_page_text(response.text))
            sources.append(
                IssuerTimingSource(
                    url=url,
                    response_sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
    finally:
        if owned_client:
            requester.close()

    issuer_marker = contract.legal_name.split()[0].casefold()
    date_marker = (
        f"{month_name[contract.event_date.month]} {contract.event_date.day}, "
        f"{contract.event_date.year}"
    ).casefold()
    if any(issuer_marker not in page or date_marker not in page for page in pages):
        raise ValueError("issuer pages no longer confirm the frozen issuer and date")
    combined = " ".join(pages)
    timing_pattern = {
        EventTiming.AFTER_MARKET_CLOSE: r"after\s+(?:the\s+)?market\s+close(?:s)?",
        EventTiming.BEFORE_MARKET_OPEN: r"before\s+(?:the\s+)?market\s+open(?:s)?",
    }.get(contract.event_timing)
    if timing_pattern is None or re.search(timing_pattern, combined) is None:
        raise ValueError("issuer pages no longer confirm the frozen release timing")
    call_local = contract.conference_call_at.astimezone(
        ZoneInfo(contract.local_timezone)
    )
    hour = str(int(call_local.strftime("%I")))
    minute = call_local.strftime("%M")
    meridiem = call_local.strftime("%p").casefold()[0]
    timezone_pattern = (
        r"(?:e\.?t\.?|est|edt|eastern\s+time)"
        if contract.local_timezone == "America/New_York"
        else r"(?:p\.?t\.?|pst|pdt|pacific\s+time)"
    )
    call_pattern = (
        rf"\b{hour}:{minute}\s*(?:{meridiem}\.?m\.?|{meridiem}m)\s*"
        rf"{timezone_pattern}"
    )
    if re.search(call_pattern, combined) is None:
        raise ValueError("issuer pages no longer confirm the frozen conference-call time")
    return IssuerTimingRevalidation(
        manifest_sha256=contract.manifest_sha256,
        checked_at=checked_at,
        event_date=contract.event_date,
        expected_release_at=contract.expected_release_at,
        conference_call_at=contract.conference_call_at,
        sources=tuple(sources),
    )


def collect_live_evidence(
    contract: LiveEvidenceContract,
    registry: FreeFirstProviderRegistry,
    issuer_timing: IssuerTimingRevalidation,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RetainedLiveEvidence:
    collected_at = contract.require_collection_authorization(clock())
    _validate_issuer_timing(contract, issuer_timing, collected_at=collected_at)
    if registry.fixture is not None:
        raise ValueError("live collection registry must not include a fixture provider")
    if registry.finnhub is None:
        raise ValueError("Finnhub is required for target event revalidation")
    budget = _request_budget(contract)
    if (
        budget.total_reserved > contract.hard_total_request_ceiling
        or budget.keyed_reserved > contract.hard_keyed_request_ceiling
    ):
        raise ValueError("planned live collection exceeds the frozen request budget")
    event = registry.finnhub.get_earnings_events(
        contract.ticker, contract.provider_window_start, contract.provider_window_end
    )
    select_target_event(contract, event)
    failures: list[LiveEvidenceFailure] = []
    expectations = _optional_call(
        "expectations",
        failures,
        lambda: (
            registry.alpha_vantage.get_earnings_events(
                contract.ticker,
                contract.provider_window_start,
                contract.provider_window_end,
            )
            if registry.alpha_vantage is not None
            else None
        ),
    )
    filings = _optional_call(
        "filings",
        failures,
        lambda: (
            registry.sec.get_earnings_filings(
                contract.ticker, contract.frozen_on, contract.event_date
            )
            if registry.sec is not None
            else None
        ),
    )
    news = _optional_call("news", failures, lambda: _collect_news(contract, registry))
    market_start = contract.event_date.replace(year=contract.event_date.year - 1)
    market_end = date.fromordinal(contract.information_cutoff_at.date().toordinal() - 1)
    security_market = _optional_call(
        "security_market",
        failures,
        lambda: (
            registry.yfinance.get_daily_history(
                contract.ticker, market_start, market_end
            )
            if registry.yfinance is not None
            else None
        ),
    )
    benchmark_market = _optional_call(
        "benchmark_market",
        failures,
        lambda: (
            registry.yfinance.get_daily_history(
                contract.benchmark_ticker, market_start, market_end
            )
            if registry.yfinance is not None
            else None
        ),
    )
    return RetainedLiveEvidence(
        collection_id=f"{contract.manifest_id}:receipts-v1",
        manifest_id=contract.manifest_id,
        manifest_sha256=contract.manifest_sha256,
        ticker=contract.ticker,
        benchmark_ticker=contract.benchmark_ticker,
        information_cutoff_at=contract.information_cutoff_at,
        collected_at=collected_at,
        issuer_timing=issuer_timing,
        budget=budget,
        event=event,
        expectations=expectations,
        filings=filings,
        news=news,
        security_market=security_market,
        benchmark_market=benchmark_market,
        failures=tuple(sorted(failures, key=lambda item: item.component)),
    )


def select_target_event(
    contract: LiveEvidenceContract, response: ProviderResponse[EarningsEvents]
) -> EarningsEventRecord:
    matches = tuple(
        item
        for item in response.data.events
        if item.ticker == contract.ticker and item.event_date == contract.event_date
    )
    if len(matches) != 1:
        raise ValueError("provider must return exactly one frozen target event")
    event = matches[0]
    if event.reported_eps is not None:
        raise ValueError("target outcome appeared before live evidence lock")
    if event.timing is not contract.event_timing:
        raise ValueError("provider no longer confirms the frozen event timing")
    if (
        event.fiscal_quarter is not None
        and _normalize_fiscal_period(event.fiscal_quarter) != contract.fiscal_period
    ):
        raise ValueError("provider target fiscal period no longer matches the manifest")
    if (
        event.fiscal_period_end is not None
        and event.fiscal_period_end != contract.fiscal_period_ended
    ):
        raise ValueError(
            "provider target fiscal period end no longer matches the manifest"
        )
    return event


def _collect_news(
    contract: LiveEvidenceContract, registry: FreeFirstProviderRegistry
) -> ProviderResponse[CompanyNewsMetadata] | None:
    provider = (
        registry.massive
        if contract.planned_news_source == "massive_news"
        else registry.alpha_vantage
    )
    if provider is None:
        return None
    return provider.get_company_news(
        contract.ticker,
        contract.provider_window_start,
        contract.provider_window_end,
        cutoff_at=contract.information_cutoff_at,
    )


def _request_budget(contract: LiveEvidenceContract) -> RequestBudgetReceipt:
    uses = tuple(
        sorted(
            (
                RequestBudgetUse(
                    source="alpha_vantage_earnings", count=1, credentialed=True
                ),
                RequestBudgetUse(
                    source=contract.planned_news_source, count=1, credentialed=True
                ),
                RequestBudgetUse(
                    source="finnhub_earnings_calendar", count=1, credentialed=True
                ),
                RequestBudgetUse(
                    source=contract.issuer_timing_source,
                    count=2,
                    credentialed=False,
                ),
                RequestBudgetUse(source="sec_edgar", count=2, credentialed=False),
                RequestBudgetUse(source="yfinance", count=2, credentialed=False),
            ),
            key=lambda item: item.source,
        )
    )
    return RequestBudgetReceipt(
        uses=uses,
        total_reserved=sum(item.count for item in uses),
        keyed_reserved=sum(item.count for item in uses if item.credentialed),
    )


def _validate_issuer_timing(
    contract: LiveEvidenceContract,
    receipt: IssuerTimingRevalidation,
    *,
    collected_at: datetime,
) -> None:
    identity = (
        receipt.manifest_sha256,
        receipt.event_date,
        receipt.expected_release_at,
        receipt.conference_call_at,
    )
    expected = (
        contract.manifest_sha256,
        contract.event_date,
        contract.expected_release_at,
        contract.conference_call_at,
    )
    if identity != expected:
        raise ValueError("issuer timing revalidation no longer matches frozen event")
    if tuple(item.url for item in receipt.sources) != contract.issuer_timing_urls:
        raise ValueError("issuer timing revalidation sources do not match the manifest")
    if receipt.checked_at > collected_at:
        raise ValueError("issuer timing cannot be checked after evidence collection")
    local_zone = ZoneInfo(contract.local_timezone)
    if receipt.checked_at.astimezone(local_zone).date() != collected_at.astimezone(
        local_zone
    ).date():
        raise ValueError("issuer timing must be revalidated on the collection date")
    if receipt.checked_at >= contract.information_cutoff_at:
        raise ValueError("issuer timing must be revalidated before cutoff")


def _optional_call(component: str, failures: list[LiveEvidenceFailure], call):
    try:
        response = call()
        if response is None:
            raise ValueError("provider is not configured")
        return response
    except (
        ProviderError,
        ProviderHTTPError,
        ProviderQuotaExceededError,
        ValueError,
    ) as error:
        failures.append(
            LiveEvidenceFailure(
                component=component,
                error_type=type(error).__name__,
                detail=str(error),
            )
        )
        return None


def _normalize_fiscal_period(value: str) -> str:
    normalized = value.upper().replace("-", "").replace(" ", "")
    return normalized if normalized.startswith("FY") else f"FY{normalized}"


def _normalized_page_text(value: str) -> str:
    without_markup = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(without_markup).casefold().split())


__all__ = [
    "collect_live_evidence",
    "revalidate_issuer_timing",
    "select_target_event",
]
