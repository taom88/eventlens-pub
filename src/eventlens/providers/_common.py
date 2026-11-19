"""Shared provider normalization helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ProviderPayloadError
from .http import HttpFetch, raw_sha256
from .models import DataQualityStatus, ProviderProvenance, ProviderWarning


def provenance(
    *,
    provider_name: str,
    provider_version: str,
    fetch: HttpFetch,
    quality: DataQualityStatus,
    coverage_start: date | None = None,
    coverage_end: date | None = None,
    warnings: tuple[ProviderWarning, ...] = (),
) -> ProviderProvenance:
    return ProviderProvenance(
        provider_name=provider_name,
        provider_version=provider_version,
        origin=fetch.origin,
        retrieved_at=fetch.retrieved_at,
        request_parameters=fetch.safe_request_parameters,
        request_fingerprint=fetch.request_fingerprint,
        raw_response_sha256=raw_sha256(fetch),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        quota_consumed=fetch.quota_consumed,
        data_quality=quality,
        warnings=warnings,
    )


def normalized_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        raise ValueError("ticker must contain letters, digits, dots, or hyphens")
    return ticker


def strict_utc_cutoff(value: datetime) -> datetime:
    """Require a cutoff with an explicit UTC offset and normalize it to UTC.

    A news cutoff is a historical-data boundary, so accepting a naive or
    non-UTC wall-clock value would make the boundary dependent on the caller's
    local timezone.  Zero-offset timezone implementations (for example
    ``timezone.utc`` and ``ZoneInfo('UTC')``) are equivalent and accepted.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cutoff_at must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("cutoff_at must use UTC")
    return value.astimezone(timezone.utc)


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "" or value == "None":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ProviderPayloadError("provider returned an invalid decimal value") from error


def require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderPayloadError(f"{context} response must be a JSON object")
    return value
