"""Provider-bound input manifests for deterministic event analytics."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventlens.providers.models import (
    EarningsEvents,
    FundamentalSnapshot,
    MarketHistory,
    ProviderOrigin,
    ProviderResponse,
)

from .backtest import EventStudyReport, run_event_study


class AnalyticsSourceRole(StrEnum):
    EVENTS = "events"
    OHLCV = "ohlcv"
    OPTIONS = "options"
    FUNDAMENTALS = "fundamentals"
    BENCHMARK = "benchmark"


class AnalyticsEligibility(StrEnum):
    ELIGIBLE = "eligible"
    DEGRADED_APPROVED = "degraded_approved"
    BLOCKED = "blocked"


class AnalyticsSourceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AnalyticsSourceRole
    provider_name: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    origin: ProviderOrigin
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retrieved_at: datetime
    coverage_start: Any | None = None
    coverage_end: Any | None = None
    quota_consumed: bool
    data_quality: str = Field(min_length=1)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def retrieved_at_is_utc(self) -> "AnalyticsSourceReceipt":
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("source receipt retrieved_at must be timezone-aware")
        object.__setattr__(self, "retrieved_at", self.retrieved_at.astimezone(timezone.utc))
        return self


class EventAnalyticsInputManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    event_ids: tuple[str, ...] = Field(min_length=1)
    information_cutoff_at: datetime
    market: str = Field(min_length=1)
    exchange_calendar: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    receipts: tuple[AnalyticsSourceReceipt, ...] = Field(min_length=2)
    policy_version: str = Field(min_length=1)
    normalized_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    eligibility: AnalyticsEligibility
    eligibility_reasons: tuple[str, ...] = ()


class EventAnalyticsInputBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: ProviderResponse[EarningsEvents]
    history: ProviderResponse[MarketHistory]
    manifest: EventAnalyticsInputManifest
    fundamentals: ProviderResponse[FundamentalSnapshot] | None = None


def _receipt(role: AnalyticsSourceRole, response: ProviderResponse[Any]) -> AnalyticsSourceReceipt:
    provenance = response.provenance
    return AnalyticsSourceReceipt(
        role=role,
        provider_name=provenance.provider_name,
        provider_version=provenance.provider_version,
        origin=provenance.origin,
        request_fingerprint=provenance.request_fingerprint,
        raw_response_sha256=provenance.raw_response_sha256,
        retrieved_at=provenance.retrieved_at,
        coverage_start=provenance.coverage_start,
        coverage_end=provenance.coverage_end,
        quota_consumed=provenance.quota_consumed,
        data_quality=provenance.data_quality.value,
        warnings=tuple(item.code for item in provenance.warnings),
    )


def assemble_event_analytics_inputs(
    events: ProviderResponse[EarningsEvents],
    history: ProviderResponse[MarketHistory],
    *,
    information_cutoff_at: datetime,
    fundamentals: ProviderResponse[FundamentalSnapshot] | None = None,
    allow_degraded: bool = False,
    market: str = "US",
    exchange_calendar: str = "XNYS",
    currency: str = "USD",
    policy_version: str = "m2.5-input-manifest-v1",
) -> EventAnalyticsInputBundle:
    """Validate provider alignment and retain an immutable eligibility manifest."""

    if information_cutoff_at.tzinfo is None or information_cutoff_at.utcoffset() is None:
        raise ValueError("information_cutoff_at must be timezone-aware")
    ticker = events.data.ticker.upper()
    if history.data.ticker.upper() != ticker:
        raise ValueError("event and market-history tickers must match")
    if fundamentals is not None and fundamentals.data.ticker.upper() != ticker:
        raise ValueError("fundamentals ticker must match event ticker")
    event_ids = tuple(item.source_event_id for item in events.data.events)
    if not event_ids or len(event_ids) != len(set(event_ids)):
        raise ValueError("event provider response must contain unique event IDs")
    receipts = [_receipt(AnalyticsSourceRole.EVENTS, events), _receipt(AnalyticsSourceRole.OHLCV, history)]
    if fundamentals is not None:
        receipts.append(_receipt(AnalyticsSourceRole.FUNDAMENTALS, fundamentals))
    reasons = [
        f"{receipt.role.value}:{receipt.data_quality}"
        for receipt in receipts
        if receipt.data_quality != "complete"
    ]
    eligibility = AnalyticsEligibility.ELIGIBLE
    if reasons:
        if not allow_degraded:
            eligibility = AnalyticsEligibility.BLOCKED
        else:
            eligibility = AnalyticsEligibility.DEGRADED_APPROVED
            reasons.append("degraded input explicitly approved by caller")
    normalized_payload = {
        "ticker": ticker,
        "events": events.data.model_dump(mode="json"),
        "history": history.data.model_dump(mode="json"),
        "fundamentals": fundamentals.data.model_dump(mode="json") if fundamentals else None,
        "receipts": [item.model_dump(mode="json") for item in receipts],
        "cutoff": information_cutoff_at.astimezone(timezone.utc).isoformat(),
        "market": market,
        "exchange_calendar": exchange_calendar,
        "currency": currency,
        "policy_version": policy_version,
    }
    input_sha = hashlib.sha256(
        json.dumps(normalized_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    manifest = EventAnalyticsInputManifest(
        ticker=ticker,
        event_ids=event_ids,
        information_cutoff_at=information_cutoff_at.astimezone(timezone.utc),
        market=market,
        exchange_calendar=exchange_calendar,
        currency=currency,
        receipts=tuple(receipts),
        policy_version=policy_version,
        normalized_input_sha256=input_sha,
        eligibility=eligibility,
        eligibility_reasons=tuple(reasons),
    )
    if eligibility is AnalyticsEligibility.BLOCKED:
        raise ValueError("analytics inputs are degraded or unavailable; explicit approval is required")
    return EventAnalyticsInputBundle(events=events, history=history, fundamentals=fundamentals, manifest=manifest)


def run_event_study_from_bundle(
    bundle: EventAnalyticsInputBundle,
    *,
    seed: int = 20260929,
    minimum_ci_observations: int = 4,
) -> tuple[EventStudyReport, EventAnalyticsInputManifest]:
    """Run existing canonical analytics without discarding the provider manifest."""

    report = run_event_study(
        bundle.manifest.ticker,
        bundle.events.data.events,
        bundle.history.data,
        seed=seed,
        minimum_ci_observations=minimum_ci_observations,
    )
    return report, bundle.manifest


__all__ = [
    "AnalyticsEligibility",
    "AnalyticsSourceReceipt",
    "AnalyticsSourceRole",
    "EventAnalyticsInputBundle",
    "EventAnalyticsInputManifest",
    "assemble_event_analytics_inputs",
    "run_event_study_from_bundle",
]
