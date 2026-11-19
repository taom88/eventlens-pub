"""Key-free D0 fixture replay for the DEMO acceptance journey.

The replay is deliberately independent of provider registries and reasoning
adapters.  It reads a checked-in, checksum-addressed fixture family, assembles
the same external-agent bundle used by the product, and sends the checked-in
report through :class:`ExternalAgentHandoffStore` and its production validator.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.agents import (
    Capability,
    CanonicalMetric,
    ExternalAgentBundle,
    ExternalAgentHandoffStore,
    ExternalAgentReport,
    ExternalEvidence,
    ExternalHandoffState,
    ExternalHandoffStatus,
    HistoricalContextRef,
    assemble_external_bundle,
    parse_external_report_json,
    validate_external_report,
)
from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.config import AgentRoute


FIXTURE_SCHEMA_VERSION = "d0-demo-replay-v1"
DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "d0" / "demo"
DEFAULT_SESSION_ID = "d0-fixture-demo-2026q3"
D0_INFORMATION_CUTOFF = datetime(2026, 10, 24, 16, tzinfo=timezone.utc)


class FixtureReplayError(ValueError):
    """Raised when a D0 fixture is incomplete, altered, or unsafe to replay."""


class FixtureMissingValue(BaseModel):
    """A persisted D0 missing value; nulls are never silently treated as zero."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FixturePreEventFeatures(BaseModel):
    """Cutoff-bounded features retained by a D0 historical card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    article_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    mean_ticker_relevance: Decimal | None = None
    weighted_sentiment: Decimal | None = None
    recency_weighted_sentiment: Decimal | None = None
    sentiment_positive_share: Decimal | None = None
    latest_close: Decimal | None = None
    prior_5d_return_pct: Decimal | None = None
    prior_20d_return_pct: Decimal | None = None
    missing_values: tuple[FixtureMissingValue, ...] = ()

    @model_validator(mode="after")
    def nulls_have_explicit_reasons(self) -> "FixturePreEventFeatures":
        reasons = {item.field: item.reason for item in self.missing_values}
        tracked = (
            "mean_ticker_relevance",
            "weighted_sentiment",
            "recency_weighted_sentiment",
            "sentiment_positive_share",
            "latest_close",
            "prior_5d_return_pct",
            "prior_20d_return_pct",
        )
        for field in tracked:
            value = getattr(self, field)
            if value is None and field not in reasons:
                raise ValueError(f"missing pre-event feature {field} requires a reason")
            if value is not None and field in reasons:
                raise ValueError(f"present pre-event feature {field} cannot have a missing reason")
        if len(reasons) != len(self.missing_values):
            raise ValueError("pre-event feature missingness contains duplicate fields")
        return self


class FixtureReactionMetrics(BaseModel):
    """Canonical signed reaction windows and explicit unavailable windows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    after_hours_return_pct: Decimal | None = None
    next_session_open_vs_reference_close_pct: Decimal | None = None
    t_plus_1_close_return_pct: Decimal | None = None
    t_plus_5_close_return_pct: Decimal | None = None
    absolute_next_session_open_move_pct: Decimal | None = Field(default=None, ge=0)
    missing_values: tuple[FixtureMissingValue, ...] = ()

    @model_validator(mode="after")
    def reaction_missingness_and_absolute_match(self) -> "FixtureReactionMetrics":
        reasons = {item.field: item.reason for item in self.missing_values}
        tracked = (
            "after_hours_return_pct",
            "next_session_open_vs_reference_close_pct",
            "t_plus_1_close_return_pct",
            "t_plus_5_close_return_pct",
            "absolute_next_session_open_move_pct",
        )
        for field in tracked:
            value = getattr(self, field)
            if value is None and field not in reasons:
                raise ValueError(f"missing reaction metric {field} requires a reason")
            if value is not None and field in reasons:
                raise ValueError(f"present reaction metric {field} cannot have a missing reason")
        if len(reasons) != len(self.missing_values):
            raise ValueError("reaction missingness contains duplicate fields")
        signed = self.next_session_open_vs_reference_close_pct
        absolute = self.absolute_next_session_open_move_pct
        if signed is not None and absolute != abs(signed):
            raise ValueError("absolute next-open move must equal abs(signed next-open move)")
        return self


class FixtureContextCard(BaseModel):
    """A human-readable, cutoff-safe historical context card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str = Field(min_length=1)
    ticker: str = "DEMO"
    event_id: str = Field(min_length=1)
    event_date: date
    outcome_available_at: datetime
    knowledge_cutoff_at: datetime
    available_at: datetime
    executive_summary: str = Field(min_length=1)
    key_messages: tuple[str, ...] = Field(min_length=1)
    metric_interpretation: str = Field(min_length=1)
    pre_event_features: FixturePreEventFeatures
    reaction_metrics: FixtureReactionMetrics
    data_quality_caveats: tuple[str, ...] = Field(min_length=1)
    provenance_links: tuple[str, ...] = Field(min_length=1)
    origin: str = "fixture_replay"
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("outcome_available_at", "knowledge_cutoff_at", "available_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fixture context timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def checksum_and_origin(self) -> "FixtureContextCard":
        if self.origin != "fixture_replay":
            raise ValueError("D0 context cards must be marked fixture_replay")
        if self.knowledge_cutoff_at <= self.outcome_available_at:
            raise ValueError(
                f"context card {self.card_id} knowledge cutoff must follow outcome availability"
            )
        if self.available_at != self.knowledge_cutoff_at:
            raise ValueError(f"context card {self.card_id} available_at must equal knowledge_cutoff_at")
        if self.knowledge_cutoff_at >= D0_INFORMATION_CUTOFF:
            raise ValueError(f"context card {self.card_id} knowledge cutoff must precede target cutoff")
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if sha256_hex(payload) != self.content_sha256:
            raise ValueError(f"context card {self.card_id} checksum mismatch")
        if not all(link.startswith("fixture://") for link in self.provenance_links):
            raise ValueError("fixture provenance links must use fixture:// URIs")
        return self


class MechanicalGuardrail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    threshold: Decimal = Field(ge=0)
    label: str = Field(min_length=1)
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def is_not_valuation(self) -> "MechanicalGuardrail":
        if "not valuation" not in self.basis.casefold():
            raise ValueError("mechanical guardrails must explicitly state they are not valuation")
        return self


class FixtureBaseline(BaseModel):
    """Fixed D0 descriptive baseline; it is not a valuation or recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_id: str = Field(min_length=1)
    ticker: str = "DEMO"
    event_id: str = Field(min_length=1)
    origin: str = "fixture_replay"
    horizon: str = Field(min_length=1)
    signed_move_pct: Decimal
    absolute_move_pct: Decimal = Field(ge=0)
    sample_size: int = Field(ge=0)
    confidence_interval: tuple[Decimal, Decimal] | None = None
    confidence_interval_missing_reason: str | None = None
    mechanical_guardrails: tuple[MechanicalGuardrail, ...] = Field(min_length=1)
    guardrails_are_not_valuation: bool = True
    baseline_note: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def fixed_and_explicit(self) -> "FixtureBaseline":
        if self.origin != "fixture_replay":
            raise ValueError("D0 baseline must be marked fixture_replay")
        if self.absolute_move_pct != abs(self.signed_move_pct):
            raise ValueError("absolute_move_pct must equal abs(signed_move_pct)")
        if self.confidence_interval is None and not self.confidence_interval_missing_reason:
            raise ValueError("missing confidence intervals require a reason")
        if not self.guardrails_are_not_valuation:
            raise ValueError("D0 guardrails cannot be presented as valuation")
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if sha256_hex(payload) != self.content_sha256:
            raise ValueError("fixture baseline checksum mismatch")
        return self


class FixtureManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = FIXTURE_SCHEMA_VERSION
    fixture_id: str = Field(min_length=1)
    ticker: str = "DEMO"
    origin: str = "fixture_replay"
    session_id: str = Field(min_length=1)
    card_files: tuple[str, ...] = Field(min_length=3, max_length=3)
    baseline_file: str = Field(min_length=1)
    bundle_file: str = Field(min_length=1)
    report_file: str = Field(min_length=1)
    checksums: dict[str, str]
    provider_calls: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    unavailable_stages: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def fixture_only(self) -> "FixtureManifest":
        if self.origin != "fixture_replay":
            raise ValueError("D0 manifest must be marked fixture_replay")
        if self.provider_calls != 0 or self.model_calls != 0:
            raise ValueError("D0 replay manifest must record zero provider/model calls")
        if len(set(self.card_files)) != 3:
            raise ValueError("D0 manifest must name exactly three unique context cards")
        return self


class FixtureReplayPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    root: Path
    manifest: FixtureManifest
    cards: tuple[FixtureContextCard, ...]
    baseline: FixtureBaseline
    bundle: ExternalAgentBundle
    report: ExternalAgentReport
    bundle_bytes: bytes
    report_bytes: bytes


@dataclass(frozen=True)
class FixtureReplayRun:
    package: FixtureReplayPackage
    exported: ExternalHandoffState
    imported: ExternalHandoffState
    reopened: ExternalHandoffState


@dataclass(frozen=True)
class FixtureReplayPreparation:
    """Prepared bundle and persisted pause state before report import."""

    package: FixtureReplayPackage
    exported: ExternalHandoffState


def load_d0_demo_fixture(root: Path | None = None) -> FixtureReplayPackage:
    """Load and verify the canonical D0 DEMO family without network access."""

    fixture_root = _resolve_fixture_root(root)
    if not fixture_root.is_dir():
        raise FixtureReplayError(f"D0 fixture directory is missing: {fixture_root}")
    manifest = _load_model(fixture_root / "manifest.json", FixtureManifest)
    if manifest.schema_version != FIXTURE_SCHEMA_VERSION or manifest.ticker != "DEMO":
        raise FixtureReplayError("unsupported D0 DEMO fixture manifest")

    cards = tuple(
        _load_model(fixture_root / filename, FixtureContextCard)
        for filename in manifest.card_files
    )
    if len(cards) != 3:
        raise FixtureReplayError("D0 DEMO replay requires exactly three prior context cards")
    if len({card.event_id for card in cards}) != 3:
        raise FixtureReplayError("D0 context cards must represent three distinct events")
    if any(card.knowledge_cutoff_at >= D0_INFORMATION_CUTOFF for card in cards):
        raise FixtureReplayError("D0 context card knowledge cutoff must precede target cutoff")
    baseline = _load_model(fixture_root / manifest.baseline_file, FixtureBaseline)
    if baseline.event_id != "DEMO:2026-10-29:2026Q3":
        raise FixtureReplayError("D0 baseline is bound to the wrong target event")
    _verify_manifest_files(fixture_root, manifest)
    bundle = _assemble_fixture_bundle(manifest, cards, baseline)
    bundle_bytes = (fixture_root / manifest.bundle_file).read_bytes()
    if bundle_bytes.rstrip(b"\n") != bundle.canonical_bytes():
        raise FixtureReplayError("checked-in D0 bundle differs from assembled bundle")
    report_bytes = (fixture_root / manifest.report_file).read_bytes()
    report = parse_external_report_json(report_bytes)
    try:
        validate_external_report(bundle, report)
    except ValueError as error:
        raise FixtureReplayError("checked-in D0 report failed production validation") from error
    return FixtureReplayPackage(
        root=fixture_root,
        manifest=manifest,
        cards=cards,
        baseline=baseline,
        bundle=bundle,
        report=report,
        bundle_bytes=bundle_bytes,
        report_bytes=report_bytes,
    )


def run_d0_demo_fixture_replay(
    store_root: Path,
    *,
    fixture_root: Path | None = None,
    session_id: str | None = None,
) -> FixtureReplayRun:
    """Export, pause, import, and reopen the key-free D0 journey.

    This function has no provider or model dependency.  It only reads the
    checked-in family and delegates persistence/validation to production code.
    """

    preparation = prepare_d0_demo_fixture_replay(
        store_root, fixture_root=fixture_root, session_id=session_id
    )
    package = preparation.package
    exported = preparation.exported
    store = ExternalAgentHandoffStore(store_root)
    if exported.status is ExternalHandoffStatus.AWAITING_EXTERNAL_AGENT:
        imported, _ = store.import_report(package.manifest.session_id, package.report_bytes)
    else:
        imported = exported
    reopened = ExternalAgentHandoffStore(store_root).load(package.manifest.session_id)
    if imported.model_dump(mode="json") != reopened.model_dump(mode="json"):
        raise FixtureReplayError("D0 handoff state changed across store restart")
    return FixtureReplayRun(package=package, exported=exported, imported=imported, reopened=reopened)


def prepare_d0_demo_fixture_replay(
    store_root: Path,
    *,
    fixture_root: Path | None = None,
    session_id: str | None = None,
) -> FixtureReplayPreparation:
    """Assemble/export the D0 bundle and stop before importing a report."""

    package = load_d0_demo_fixture(fixture_root)
    if session_id is not None and session_id != package.manifest.session_id:
        raise FixtureReplayError("D0 fixture session_id is immutable")
    exported = ExternalAgentHandoffStore(store_root).export(package.bundle)
    return FixtureReplayPreparation(package=package, exported=exported)


def _load_model(path: Path, model: type[BaseModel]):
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise FixtureReplayError(f"invalid D0 fixture file {path.name}: {error}") from error


def _verify_manifest_files(root: Path, manifest: FixtureManifest) -> None:
    files = (*manifest.card_files, manifest.baseline_file, manifest.bundle_file, manifest.report_file)
    for filename in files:
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            raise FixtureReplayError(f"unsafe D0 fixture path: {filename}")
        path = root / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest.checksums.get(filename) != digest:
            raise FixtureReplayError(f"D0 fixture checksum mismatch: {filename}")


def _resolve_fixture_root(root: Path | None) -> Path:
    if root is not None:
        return root.resolve()
    if DEFAULT_FIXTURE_ROOT.is_dir():
        return DEFAULT_FIXTURE_ROOT.resolve()
    packaged = files("eventlens").joinpath("fixtures", "d0", "demo")
    try:
        return Path(str(packaged)).resolve()
    except (OSError, TypeError) as error:
        raise FixtureReplayError("bundled D0 demo fixture is unavailable") from error


def _assemble_fixture_bundle(
    manifest: FixtureManifest,
    cards: tuple[FixtureContextCard, ...],
    baseline: FixtureBaseline,
) -> ExternalAgentBundle:
    cutoff = D0_INFORMATION_CUTOFF
    release = datetime(2026, 10, 29, 20, tzinfo=timezone.utc)
    evidence = tuple(
        ExternalEvidence(
            source_id=f"eventlens-context-card:{card.card_id}",
            passage_id="executive-summary",
            evidence_kind="historical_context",
            typed_summary=card.executive_summary,
            available_at=card.available_at,
            content_sha256=sha256_hex(card.executive_summary),
            rights_status="summary_permitted",
            retention_status="summary_only",
            provenance_ref=card.card_id,
        )
        for card in cards
    )
    history = tuple(
        HistoricalContextRef(
            card_id=card.card_id,
            event_id=card.event_id,
            event_date=card.event_date,
            knowledge_cutoff_at=card.knowledge_cutoff_at,
            card_sha256=card.content_sha256,
            readable_summary=card.executive_summary,
            key_messages=card.key_messages,
            caveats=card.data_quality_caveats,
        )
        for card in cards
    )
    return assemble_external_bundle(
        bundle_id=f"{manifest.session_id}:external-agent:r1",
        session_id=manifest.session_id,
        event_id=baseline.event_id,
        ticker="DEMO",
        route=AgentRoute.EXTERNAL,
        bundle_revision=1,
        built_at=cutoff,
        information_cutoff_at=cutoff,
        target_release_at=release,
        target_release_status="fixture_assumed_after_market_close",
        research_question="Prepare a key-free D0 fixture replay for Example Systems's upcoming earnings.",
        instructions=(
            "Keep signed and absolute reaction labels separate.",
            "Treat mechanical guardrails as not valuation conclusions.",
            "Separate facts, implications, uncertainty, and missing evidence.",
        ),
        canonical_metrics=(
            CanonicalMetric(
                metric_id="fixture_baseline_signed_move_pct",
                value=baseline.signed_move_pct,
                unit="PERCENT",
                period=baseline.horizon,
                as_of=cutoff,
                calculation_version="d0-fixture-baseline-v1",
                provenance_ids=("fixture-replay:baseline",),
            ),
        ),
        evidence=evidence,
        historical_context=history,
        missingness=(
            "confidence interval unavailable: insufficient sample size (n=3)",
            "provider-native sentiment unavailable in fixture_replay",
            "company-specific valuation unavailable in D0",
        ),
        warnings=("synthetic demonstration data only; not live market history",),
        unavailable_stages=manifest.unavailable_stages,
        allowed_skills=(Capability(capability_id="fixture_replay", version="1"),),
        allowed_tools=(),
        forbidden_behaviors=(
            "use post-cutoff evidence",
            "treat fixture provenance as live provenance",
            "present mechanical guardrails as valuation",
            "provide execution instructions",
        ),
    )


__all__ = [
    "DEFAULT_FIXTURE_ROOT",
    "FIXTURE_SCHEMA_VERSION",
    "FixtureBaseline",
    "FixtureContextCard",
    "FixtureMissingValue",
    "FixturePreEventFeatures",
    "FixtureReactionMetrics",
    "FixtureManifest",
    "FixtureReplayError",
    "FixtureReplayPackage",
    "FixtureReplayPreparation",
    "FixtureReplayRun",
    "load_d0_demo_fixture",
    "prepare_d0_demo_fixture_replay",
    "run_d0_demo_fixture_replay",
]
