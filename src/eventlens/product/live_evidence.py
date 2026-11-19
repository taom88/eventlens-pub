"""Authorization-gated collection of cutoff-safe live provider receipts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.domain.events import EventTiming
from eventlens.providers.models import (
    CompanyNewsMetadata,
    EarningsEvents,
    FilingEvidence,
    MarketHistory,
    ProviderOrigin,
    ProviderResponse,
)

LIVE_EVIDENCE_SCHEMA_VERSION = "eventlens.live-evidence-receipts.v1"
LIVE_ACCEPTANCE_CONFIG_VERSION = "eventlens.acceptance_run_config.v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class LiveEvidenceContract(_Frozen):
    manifest_id: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_relative_path: str
    ticker: str
    legal_name: str
    ontology_id: str
    fiscal_period: str
    fiscal_period_ended: date
    event_date: date
    event_timing: EventTiming
    expected_release_at: datetime
    conference_call_at: datetime
    information_cutoff_at: datetime
    local_timezone: str
    provider_window_start: date
    provider_window_end: date
    benchmark_ticker: str
    market: str
    exchange_calendar: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    sec_submissions_url: str
    frozen_on: date
    output_root: Path
    analyst_actor: str
    live_provider_run_authorized: bool
    issuer_timing_urls: tuple[str, str]
    issuer_timing_source: str
    hard_total_request_ceiling: int = Field(ge=1)
    hard_keyed_request_ceiling: int = Field(ge=1)
    planned_news_source: Literal["alpha_vantage_news", "massive_news"]

    @field_validator("ticker", "benchmark_ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()

    @field_validator(
        "expected_release_at", "conference_call_at", "information_cutoff_at"
    )
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def window_is_coherent(self) -> LiveEvidenceContract:
        if not (
            self.information_cutoff_at
            < self.expected_release_at
            < self.conference_call_at
        ):
            raise ValueError("cutoff, expected release, and call must be ordered")
        if self.provider_window_end != self.event_date:
            raise ValueError("provider window must end on the target event date")
        if self.provider_window_start > self.provider_window_end:
            raise ValueError("provider window start must not follow its end")
        if self.issuer_timing_urls != tuple(sorted(set(self.issuer_timing_urls))):
            raise ValueError("issuer timing URLs must be sorted and unique")
        return self

    def require_collection_authorization(self, now: datetime) -> datetime:
        current = _utc(now)
        if not self.live_provider_run_authorized:
            raise PermissionError("live provider collection is not owner-authorized")
        local_zone = ZoneInfo(self.local_timezone)
        if (
            current.astimezone(local_zone).date()
            != self.information_cutoff_at.astimezone(local_zone).date()
        ):
            raise ValueError(
                "live provider collection must occur on the frozen collection date"
            )
        if current >= self.information_cutoff_at:
            raise ValueError(
                "live provider collection must finish strictly before cutoff"
            )
        return current

    def require_assembly_window(self, now: datetime) -> datetime:
        current = _utc(now)
        if current < self.information_cutoff_at:
            raise ValueError(
                "live bundle assembly cannot precede the information cutoff"
            )
        if current >= self.expected_release_at:
            raise ValueError("live bundle assembly must precede the target result")
        return current

    def require_replay_time(self, now: datetime) -> datetime:
        current = _utc(now)
        if current < self.expected_release_at:
            raise ValueError("retrospective replay cannot precede the target result")
        return current


class IssuerTimingSource(_Frozen):
    url: str
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retrieval_channel: Literal["direct_http", "live_search_index"] = "direct_http"
    hash_scope: Literal["raw_response", "normalized_extract"] = "raw_response"

    @model_validator(mode="after")
    def retrieval_and_hash_scope_match(self) -> IssuerTimingSource:
        expected = {
            "direct_http": "raw_response",
            "live_search_index": "normalized_extract",
        }
        if self.hash_scope != expected[self.retrieval_channel]:
            raise ValueError("issuer timing retrieval channel and hash scope conflict")
        return self


class IssuerTimingRevalidation(_Frozen):
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    checked_at: datetime
    event_date: date
    expected_release_at: datetime
    conference_call_at: datetime
    sources: tuple[IssuerTimingSource, ...] = Field(min_length=2, max_length=2)

    @field_validator("checked_at", "expected_release_at", "conference_call_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def sources_are_canonical(self) -> IssuerTimingRevalidation:
        urls = tuple(item.url for item in self.sources)
        if urls != tuple(sorted(urls)) or len(urls) != len(set(urls)):
            raise ValueError("issuer timing sources must be sorted and unique")
        return self


class RequestBudgetUse(_Frozen):
    source: str
    count: int = Field(ge=1)
    credentialed: bool


class RequestBudgetReceipt(_Frozen):
    uses: tuple[RequestBudgetUse, ...]
    total_reserved: int = Field(ge=0)
    keyed_reserved: int = Field(ge=0)

    @model_validator(mode="after")
    def totals_match(self) -> RequestBudgetReceipt:
        sources = tuple(item.source for item in self.uses)
        if sources != tuple(sorted(sources)) or len(sources) != len(set(sources)):
            raise ValueError("request budget sources must be sorted and unique")
        if self.total_reserved != sum(item.count for item in self.uses):
            raise ValueError("total request reservation does not match uses")
        if self.keyed_reserved != sum(
            item.count for item in self.uses if item.credentialed
        ):
            raise ValueError("keyed request reservation does not match uses")
        return self


class LiveEvidenceFailure(_Frozen):
    component: str
    error_type: str
    detail: str


class RetainedLiveEvidence(_Frozen):
    schema_version: Literal[LIVE_EVIDENCE_SCHEMA_VERSION] = LIVE_EVIDENCE_SCHEMA_VERSION
    collection_id: str
    manifest_id: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ticker: str
    benchmark_ticker: str
    information_cutoff_at: datetime
    collected_at: datetime
    issuer_timing: IssuerTimingRevalidation
    budget: RequestBudgetReceipt
    event: ProviderResponse[EarningsEvents]
    expectations: ProviderResponse[EarningsEvents] | None = None
    filings: ProviderResponse[FilingEvidence] | None = None
    news: ProviderResponse[CompanyNewsMetadata] | None = None
    security_market: ProviderResponse[MarketHistory] | None = None
    benchmark_market: ProviderResponse[MarketHistory] | None = None
    failures: tuple[LiveEvidenceFailure, ...] = ()
    collection_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("ticker", "benchmark_ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()

    @field_validator("information_cutoff_at", "collected_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_receipts(self) -> RetainedLiveEvidence:
        if self.collected_at >= self.information_cutoff_at:
            raise ValueError("retained live evidence must be collected before cutoff")
        if self.event.data.ticker != self.ticker:
            raise ValueError("event response ticker must match collection ticker")
        responses = tuple(
            response
            for response in (
                self.event,
                self.expectations,
                self.filings,
                self.news,
                self.security_market,
                self.benchmark_market,
            )
            if response is not None
        )
        if any(
            response.provenance.origin is ProviderOrigin.FIXTURE
            for response in responses
        ):
            raise ValueError(
                "fixture-origin provider data is forbidden in live evidence"
            )
        if any(
            response.provenance.retrieved_at >= self.information_cutoff_at
            for response in responses
        ):
            raise ValueError(
                "provider responses must be retrieved strictly before cutoff"
            )
        _validate_response_tickers(self)
        if self.news is not None and any(
            item.published_at >= self.information_cutoff_at
            for item in self.news.data.articles
        ):
            raise ValueError("news at or after cutoff is forbidden")
        if self.news is not None and any(
            item.retrieved_at is not None
            and item.retrieved_at >= self.information_cutoff_at
            for item in self.news.data.articles
        ):
            raise ValueError("news retrieved at or after cutoff is forbidden")
        for response in (self.event, self.expectations):
            if response is None:
                continue
            target_events = tuple(
                item
                for item in response.data.events
                if item.ticker.upper() == self.ticker
                and item.event_date == self.issuer_timing.event_date
            )
            if any(item.reported_eps is not None for item in target_events):
                raise ValueError("target outcome appeared in retained live evidence")
        latest_market_date = date.fromordinal(
            self.information_cutoff_at.date().toordinal() - 1
        )
        for response in (self.security_market, self.benchmark_market):
            if response is not None and any(
                bar.trading_date > latest_market_date for bar in response.data.bars
            ):
                raise ValueError("market history extends into the target event date")
        failure_keys = tuple(item.component for item in self.failures)
        if failure_keys != tuple(sorted(failure_keys)) or len(failure_keys) != len(
            set(failure_keys)
        ):
            raise ValueError("live evidence failures must be sorted and unique")
        expected = self.content_checksum()
        if self.collection_sha256 is None:
            object.__setattr__(self, "collection_sha256", expected)
        elif self.collection_sha256 != expected:
            raise ValueError("live evidence collection checksum does not match")
        return self

    def content_checksum(self) -> str:
        return sha256_hex(self.model_dump(mode="json", exclude={"collection_sha256"}))

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


def load_live_evidence_contract(
    manifest_path: Path, run_config_path: Path
) -> LiveEvidenceContract:
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    sidecar = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    if manifest_sha != sidecar.read_text(encoding="utf-8").strip():
        raise ValueError("acceptance manifest checksum does not match")
    manifest = json.loads(manifest_bytes)
    config = json.loads(run_config_path.read_text(encoding="utf-8"))
    if config.get("schema_version") != LIVE_ACCEPTANCE_CONFIG_VERSION:
        raise ValueError("unsupported live acceptance run config")
    if config.get("data_mode") != "live":
        raise ValueError("live evidence collection refuses non-live data mode")
    if config.get("manifest_relative_path") != manifest_path.as_posix():
        raise ValueError("run config manifest path does not match selected manifest")
    identity = (
        config.get("ticker"),
        config.get("event_date"),
        config.get("fiscal_period"),
        config.get("information_cutoff_at"),
    )
    expected_identity = (
        manifest["issuer"]["ticker"],
        manifest["event"]["event_date"],
        manifest["event"]["fiscal_period"],
        manifest["information_barrier"]["cutoff_at"],
    )
    if identity != expected_identity:
        raise ValueError("run config identity does not match acceptance manifest")
    ontology_id = manifest["issuer"].get("ontology_id")
    if not isinstance(ontology_id, str) or not ontology_id.strip():
        raise ValueError("acceptance manifest must declare issuer.ontology_id")
    news_sources = tuple(
        item["source"]
        for item in manifest["call_budget"]["planned_requests"]
        if item["source"] in {"alpha_vantage_news", "massive_news"}
    )
    if len(news_sources) != 1:
        raise ValueError(
            "acceptance manifest must reserve exactly one supported news source"
        )
    issuer_timing_sources = tuple(
        item["source"]
        for item in manifest["call_budget"]["planned_requests"]
        if item["source"].endswith("_investor_relations")
    )
    if len(issuer_timing_sources) != 1:
        raise ValueError(
            "acceptance manifest must reserve exactly one issuer timing source"
        )
    return LiveEvidenceContract(
        manifest_id=manifest["manifest_id"],
        manifest_sha256=manifest_sha,
        manifest_relative_path=config["manifest_relative_path"],
        ticker=manifest["issuer"]["ticker"],
        legal_name=manifest["issuer"]["legal_name"],
        ontology_id=ontology_id,
        fiscal_period=manifest["event"]["fiscal_period"],
        fiscal_period_ended=manifest["event"]["fiscal_period_ended"],
        event_date=manifest["event"]["event_date"],
        event_timing=manifest["event"].get("timing", EventTiming.AFTER_MARKET_CLOSE),
        expected_release_at=manifest["event"]["expected_results_release_at"],
        conference_call_at=manifest["event"]["conference_call_at"],
        information_cutoff_at=manifest["information_barrier"]["cutoff_at"],
        local_timezone=manifest["event"]["local_timezone"],
        provider_window_start=config["provider_window"]["start"],
        provider_window_end=config["provider_window"]["end"],
        benchmark_ticker=manifest["market"]["benchmark_ticker"],
        market="US",
        exchange_calendar=manifest["market"]["exchange_calendar"],
        currency=manifest["market"]["currency"],
        sec_submissions_url=manifest["eligibility"]["issuer_primary_material"][
            "sec_submissions_url"
        ],
        frozen_on=manifest["frozen_on"],
        output_root=config["output_root"],
        analyst_actor=config["analyst_actor"],
        live_provider_run_authorized=config["live_provider_run_authorized"],
        issuer_timing_urls=tuple(
            sorted(
                record["url"]
                for record in manifest["source_confirmation"]["records"]
                if record["confirmation_class"].startswith("issuer_")
            )
        ),
        issuer_timing_source=issuer_timing_sources[0],
        hard_total_request_ceiling=manifest["call_budget"][
            "hard_total_request_ceiling"
        ],
        hard_keyed_request_ceiling=manifest["call_budget"][
            "hard_keyed_request_ceiling"
        ],
        planned_news_source=news_sources[0],
    )


def _validate_response_tickers(evidence: RetainedLiveEvidence) -> None:
    for response in (evidence.event, evidence.expectations):
        if response is not None and any(
            item.ticker.upper() != evidence.ticker for item in response.data.events
        ):
            raise ValueError("earnings event ticker must match collection ticker")
    same_ticker = (evidence.expectations, evidence.filings, evidence.news)
    if any(
        item is not None and item.data.ticker != evidence.ticker for item in same_ticker
    ):
        raise ValueError("provider response ticker must match collection ticker")
    if (
        evidence.security_market is not None
        and evidence.security_market.data.ticker != evidence.ticker
    ):
        raise ValueError("security market ticker must match collection ticker")
    if (
        evidence.benchmark_market is not None
        and evidence.benchmark_market.data.ticker != evidence.benchmark_ticker
    ):
        raise ValueError("benchmark market ticker must match configured benchmark")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "LIVE_EVIDENCE_SCHEMA_VERSION",
    "IssuerTimingRevalidation",
    "IssuerTimingSource",
    "LiveEvidenceContract",
    "RetainedLiveEvidence",
    "load_live_evidence_contract",
]
