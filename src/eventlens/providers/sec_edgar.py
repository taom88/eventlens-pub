"""No-key SEC EDGAR filing evidence from Item 2.02 8-K filings."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from ._common import normalized_ticker, provenance, require_mapping
from .errors import ProviderConfigurationError, ProviderPayloadError
from .http import CachedHttpClient
from .models import (
    DataQualityStatus,
    FilingEvidence,
    FilingEvidenceRecord,
    ProviderResponse,
    ProviderWarning,
)


class SecEdgarProvider:
    provider_name = "sec_edgar"
    provider_version = "submissions-v1"
    ticker_map_url = "https://www.sec.gov/files/company_tickers.json"
    submissions_url = "https://data.sec.gov/submissions/CIK{cik}.json"
    minimum_interval_seconds = 0.11

    def __init__(self, user_agent: str, http: CachedHttpClient) -> None:
        if not valid_sec_user_agent(user_agent):
            raise ProviderConfigurationError(
                "SEC User-Agent must identify the application and a real contact email"
            )
        self._user_agent = user_agent.strip()
        self._http = http

    def get_earnings_filings(
        self, ticker: str, start: date, end: date
    ) -> ProviderResponse[FilingEvidence]:
        symbol = normalized_ticker(ticker)
        if end < start:
            raise ValueError("end must be on or after start")
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}
        mapping_fetch = self._http.get_json(
            provider=self.provider_name,
            url=self.ticker_map_url,
            parameters={},
            max_age_seconds=7 * 24 * 60 * 60,
            headers=headers,
            minimum_interval_seconds=self.minimum_interval_seconds,
        )
        cik = _find_cik(mapping_fetch.payload, symbol)
        fetch = self._http.get_json(
            provider=self.provider_name,
            url=self.submissions_url.format(cik=f"{cik:010d}"),
            parameters={},
            max_age_seconds=6 * 60 * 60,
            headers=headers,
            minimum_interval_seconds=self.minimum_interval_seconds,
        )
        payload = require_mapping(fetch.payload, "SEC submissions")
        recent = require_mapping(
            require_mapping(payload.get("filings"), "SEC filings").get("recent"),
            "SEC recent filings",
        )
        filings = _item_202_filings(symbol, recent, start, end)
        return ProviderResponse(
            data=FilingEvidence(ticker=symbol, filings=filings),
            provenance=provenance(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                fetch=fetch,
                quality=DataQualityStatus.PARTIAL,
                coverage_start=filings[0].filing_date if filings else None,
                coverage_end=filings[-1].filing_date if filings else None,
                warnings=(
                    ProviderWarning(
                        code="filing_date_not_release_time",
                        message=(
                            "SEC Item 2.02 filings confirm historical results but do not "
                            "replace a forward earnings calendar or establish release timing."
                        ),
                    ),
                ),
            ),
        )


_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE
)
_PLACEHOLDER_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})


def valid_sec_user_agent(user_agent: str) -> bool:
    """Require an app identity and reject documentation-only email domains."""

    normalized = user_agent.strip()
    match = _EMAIL_PATTERN.search(normalized)
    if match is None or len(normalized.split()) < 2:
        return False
    return match.group(1).casefold() not in _PLACEHOLDER_EMAIL_DOMAINS


def _find_cik(payload: Any, ticker: str) -> int:
    mapping = require_mapping(payload, "SEC ticker map")
    for value in mapping.values():
        if isinstance(value, dict) and str(value.get("ticker", "")).upper() == ticker:
            return int(value["cik_str"])
    raise ProviderPayloadError(f"SEC ticker map did not contain {ticker}")


def _item_202_filings(
    ticker: str, recent: dict[str, Any], start: date, end: date
) -> tuple[FilingEvidenceRecord, ...]:
    required = ("accessionNumber", "filingDate", "form", "items")
    if any(not isinstance(recent.get(name), list) for name in required):
        raise ProviderPayloadError("SEC recent filings arrays are incomplete")
    lengths = {len(recent[name]) for name in required}
    if len(lengths) != 1:
        raise ProviderPayloadError("SEC recent filings arrays have mismatched lengths")
    filings: list[FilingEvidenceRecord] = []
    for accession, filing_date, form, items in zip(*(recent[name] for name in required)):
        day = date.fromisoformat(str(filing_date))
        item_set = {item.strip() for item in str(items or "").split(",")}
        if str(form) == "8-K" and "2.02" in item_set and start <= day <= end:
            filings.append(
                FilingEvidenceRecord(
                    ticker=ticker,
                    filing_date=day,
                    form="8-K",
                    items=tuple(sorted(item_set)),
                    accession_number=str(accession),
                )
            )
    filings.sort(key=lambda item: item.filing_date)
    return tuple(filings)
