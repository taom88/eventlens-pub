"""Immutable, cutoff-safe inputs for company-specific research synthesis.

This module deliberately assembles already-validated facts.  It does not
retrieve providers, normalize consensus, call a model, value a company, or
choose a recommendation.  Later MVPs receive this one bounded artifact rather
than a collection of mutable provider objects.
"""

from __future__ import annotations

import os
import tempfile
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.context_cards.guidance import GuidanceEntry
from eventlens.context_cards.metrics import MetricUnit, MetricValue
from eventlens.context_cards.models import ConsensusState, ContextCardV4
from eventlens.product.company_context import CitedCompanyStatement, CompanyContextArtifact
from eventlens.product.news_signal import NewsSignal
from eventlens.quant.backtest import EventStudyReport, event_study_report_sha256
from eventlens.quant.inputs import EventAnalyticsInputManifest
from eventlens.quant.pre_event_features import PreEventFeatureSnapshot
from eventlens.quant.supplement import EventStudySupplement
from eventlens.domain.events import EventTiming


RESEARCH_EVIDENCE_SCHEMA_VERSION = "eventlens.research-evidence.v2"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _utc(value: datetime, field: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _model_sha256(value: BaseModel) -> str:
    return sha256_hex(value.model_dump(mode="json"))


def _revalidate(model: BaseModel, cls: type[BaseModel]) -> BaseModel:
    """Re-run nested validators even when a caller passed model_construct()."""

    return cls.model_validate(model.model_dump(mode="json"))


class TargetEventIdentity(_Frozen):
    ticker: str = Field(min_length=1, max_length=16)
    event_id: str = Field(min_length=1, max_length=200)
    fiscal_period: str = Field(min_length=1, max_length=80)
    event_date: date
    release_status: Literal["confirmed", "estimated", "unknown"]
    timing: EventTiming
    market: str = Field(min_length=1, max_length=40)
    exchange_calendar: str = Field(min_length=1, max_length=80)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    event_at: datetime | None = None

    @field_validator("ticker")
    @classmethod
    def ticker_upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("event_at")
    @classmethod
    def event_at_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value, "event_at") if value is not None else None

    @model_validator(mode="after")
    def release_status_matches_timestamp(self) -> "TargetEventIdentity":
        if self.release_status == "unknown" and self.event_at is not None:
            raise ValueError("unknown release status cannot carry event_at")
        if self.release_status != "unknown" and self.event_at is None:
            raise ValueError("confirmed or estimated release status requires event_at")
        if self.event_at is not None and self.event_at.date() != self.event_date:
            raise ValueError("target event_at date must match event_date")
        return self


class EvidenceReceipt(_Frozen):
    """A stable receipt for a component embedded in the facts bundle."""

    component: str = Field(min_length=1, max_length=80)
    receipt_id: str = Field(min_length=1, max_length=200)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    available_at: datetime
    computed_at: datetime | None = None
    source_ref: str = Field(min_length=1, max_length=1_000)
    quality: str = Field(default="complete", min_length=1, max_length=80)
    warnings: tuple[str, ...] = ()
    input_receipt_ids: tuple[str, ...] = ()
    provenance_refs: tuple[str, ...] = ()

    @field_validator("available_at")
    @classmethod
    def available_at_utc(cls, value: datetime) -> datetime:
        return _utc(value, "receipt available_at")

    @field_validator("computed_at")
    @classmethod
    def computed_at_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value, "receipt computed_at") if value is not None else None

    @model_validator(mode="after")
    def receipt_provenance_is_canonical(self) -> "EvidenceReceipt":
        for values, label in (
            (self.warnings, "warnings"), (self.input_receipt_ids, "input receipt IDs"),
            (self.provenance_refs, "provenance refs"),
        ):
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"receipt {label} must be sorted and unique")
        if self.computed_at is not None and self.computed_at < self.available_at:
            raise ValueError("receipt computed_at cannot precede available_at")
        return self


class OntologyCoverage(_Frozen):
    """Derived coverage only; callers cannot hand-author its counts."""

    ontology_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    ontology_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ontology_use_mode: str = Field(min_length=1)
    context_content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    critical_slots_populated: int = Field(ge=0)
    critical_slots_total: int = Field(ge=0)
    populated_slot_ids: tuple[str, ...]
    conflicted_slot_ids: tuple[str, ...]
    missing_slot_ids: tuple[str, ...]

    @model_validator(mode="after")
    def canonical_slot_lists(self) -> "OntologyCoverage":
        lists = (self.populated_slot_ids, self.conflicted_slot_ids, self.missing_slot_ids)
        if any(values != tuple(sorted(values)) or len(values) != len(set(values)) for values in lists):
            raise ValueError("ontology coverage slot IDs must be sorted and unique")
        if set(self.populated_slot_ids) & set(self.conflicted_slot_ids):
            raise ValueError("ontology coverage slots cannot be both populated and conflicted")
        if self.critical_slots_populated > self.critical_slots_total:
            raise ValueError("populated critical slots cannot exceed total")
        return self

    @classmethod
    def from_context(cls, context: CompanyContextArtifact) -> "OntologyCoverage":
        # Explicit revalidation protects against a forged model_construct object.
        context = _revalidate(context, CompanyContextArtifact)  # type: ignore[assignment]
        populated = tuple(sorted(slot.slot_id for slot in context.slots if slot.state.value == "populated"))
        conflicted = tuple(sorted(slot.slot_id for slot in context.slots if slot.state.value == "conflicted"))
        missing = tuple(sorted(slot.slot_id for slot in context.slots if slot.state.value == "missing"))
        return cls(
            ontology_id=context.ontology_id,
            ontology_version=context.ontology_version,
            ontology_sha256=context.ontology_sha256,
            ontology_use_mode=context.ontology_use_mode.value,
            context_content_sha256=context.content_sha256,
            critical_slots_populated=context.critical_slots_populated,
            critical_slots_total=context.critical_slots_total,
            populated_slot_ids=populated,
            conflicted_slot_ids=conflicted,
            missing_slot_ids=missing,
        )


class CompanyContextEvidence(_Frozen):
    context: CompanyContextArtifact
    receipt: EvidenceReceipt

    @model_validator(mode="after")
    def matches_context(self) -> "CompanyContextEvidence":
        context = _revalidate(self.context, CompanyContextArtifact)
        if self.receipt.component != "company_context" or self.receipt.content_sha256 != context.content_sha256:
            raise ValueError("company-context receipt must match the validated context artifact")
        return self


class PriorContextCardEvidence(_Frozen):
    prior_event_id: str = Field(min_length=1)
    card_id: str = Field(min_length=1)
    event_date: date
    knowledge_cutoff_at: datetime
    card: ContextCardV4
    card_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: EvidenceReceipt

    @field_validator("knowledge_cutoff_at")
    @classmethod
    def knowledge_cutoff_utc(cls, value: datetime) -> datetime:
        return _utc(value, "prior-card knowledge_cutoff_at")

    @model_validator(mode="after")
    def card_receipt_matches(self) -> "PriorContextCardEvidence":
        card = _revalidate(self.card, ContextCardV4)
        expected = _model_sha256(card)
        if self.card_sha256 != expected or self.receipt.content_sha256 != expected:
            raise ValueError("prior-card checksum must match the complete validated card")
        if self.receipt.component != "prior_context_card":
            raise ValueError("prior-card receipt component is invalid")
        if self.knowledge_cutoff_at != card.identity.card.as_of:
            raise ValueError("prior-card knowledge cutoff must match card as_of")
        if card.identity.event.event_datetime is not None and self.event_date != card.identity.event.event_datetime.date():
            raise ValueError("prior-card event date must match card identity")
        return self


class ConsensusEvidence(_Frozen):
    consensus: ConsensusState
    availability_status: Literal["missing", "partial", "available", "conflicted"]
    evidence_refs: tuple[str, ...] = ()
    consensus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: EvidenceReceipt

    @model_validator(mode="after")
    def consensus_receipt_matches(self) -> "ConsensusEvidence":
        consensus = _revalidate(self.consensus, ConsensusState)
        expected = _model_sha256(consensus)
        if self.consensus_sha256 != expected or self.receipt.content_sha256 != expected:
            raise ValueError("consensus checksum must match complete consensus state")
        if self.receipt.component != "consensus":
            raise ValueError("consensus receipt component is invalid")
        if self.availability_status != consensus.status:
            raise ValueError("consensus availability status must match consensus state")
        if self.evidence_refs != tuple(sorted(self.evidence_refs)) or len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("consensus evidence refs must be sorted and unique")
        return self


class ManagementEvidence(_Frozen):
    guidance: tuple[GuidanceEntry, ...]
    statements: tuple["CitedCompanyStatement", ...]
    guidance_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: EvidenceReceipt

    @model_validator(mode="after")
    def guidance_receipt_matches(self) -> "ManagementEvidence":
        guidance = tuple(_revalidate(item, GuidanceEntry) for item in self.guidance)
        ids = tuple(item.guidance_id for item in guidance)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("management guidance must be sorted and unique by guidance_id")
        expected = sha256_hex({
            "guidance": [item.model_dump(mode="json") for item in guidance],
            "statements": [item.model_dump(mode="json") for item in self.statements],
        })
        if self.guidance_sha256 != expected or self.receipt.content_sha256 != expected:
            raise ValueError("management-guidance checksum must match validated guidance")
        if self.receipt.component != "management_guidance":
            raise ValueError("management receipt component is invalid")
        return self


class NewsEvidence(_Frozen):
    signal: NewsSignal
    receipt: EvidenceReceipt

    @model_validator(mode="after")
    def signal_receipt_matches(self) -> "NewsEvidence":
        signal = _revalidate(self.signal, NewsSignal)
        if self.receipt.component != "news_signal" or self.receipt.content_sha256 != signal.signal_sha256:
            raise ValueError("news receipt must match the validated news signal")
        return self


class TechnicalProjection(_Frozen):
    """Market-only fields projected from legacy pre-event features.

    The source snapshot may contain provider-native news metadata.  It is
    checksum-addressed but never embedded here, so it cannot masquerade as
    the separately classified NewsSignal.
    """

    ticker: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    event_date: date
    cutoff_at: datetime
    latest_close: Decimal | None = None
    prior_5d_return_pct: Decimal | None = None
    prior_20d_return_pct: Decimal | None = None

    @field_validator("ticker")
    @classmethod
    def technical_ticker_upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("cutoff_at")
    @classmethod
    def technical_cutoff_utc(cls, value: datetime) -> datetime:
        return _utc(value, "technical cutoff_at")

    @classmethod
    def from_snapshot(cls, snapshot: PreEventFeatureSnapshot) -> "TechnicalProjection":
        snapshot = _revalidate(snapshot, PreEventFeatureSnapshot)  # type: ignore[assignment]
        if snapshot.realized_outcome_status != "not_attached" or snapshot.recommendation_status != "not_generated":
            raise ValueError("technical projection cannot include target outcomes or recommendations")
        return cls(
            ticker=snapshot.ticker, event_id=snapshot.event_id, event_date=snapshot.event_date,
            cutoff_at=snapshot.cutoff_at, latest_close=snapshot.latest_close,
            prior_5d_return_pct=snapshot.prior_5d_return_pct,
            prior_20d_return_pct=snapshot.prior_20d_return_pct,
        )


class TechnicalEvidence(_Frozen):
    """A validated feature snapshot plus its exact market-only projection.

    Retaining the source snapshot gives reload-time validation a stable source
    of truth.  The projection is deliberately redundant: it is the only
    technical surface exposed to synthesis and must be re-derived byte-for-
    byte from that snapshot on every load.
    """

    snapshot: PreEventFeatureSnapshot
    technical: TechnicalProjection
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: EvidenceReceipt

    @model_validator(mode="after")
    def snapshot_receipt_matches(self) -> "TechnicalEvidence":
        snapshot = _revalidate(self.snapshot, PreEventFeatureSnapshot)
        expected_checksum = _model_sha256(snapshot)
        expected_projection = TechnicalProjection.from_snapshot(snapshot)
        if self.technical != expected_projection:
            raise ValueError("technical projection must be derived exactly from the validated feature snapshot")
        if self.snapshot_sha256 != expected_checksum or self.receipt.content_sha256 != expected_checksum:
            raise ValueError("technical receipt must match the validated source pre-event snapshot")
        if self.receipt.component != "pre_event_features":
            raise ValueError("technical receipt component is invalid")
        return self

    @classmethod
    def from_snapshot(cls, snapshot: PreEventFeatureSnapshot, *, receipt: EvidenceReceipt) -> "TechnicalEvidence":
        projection = TechnicalProjection.from_snapshot(snapshot)
        checksum = _model_sha256(snapshot)
        return cls(snapshot=snapshot, technical=projection, snapshot_sha256=checksum, receipt=receipt)


class HistoricalAnalyticsEvidence(_Frozen):
    input_manifest: EventAnalyticsInputManifest
    event_study: EventStudyReport
    supplement: EventStudySupplement | None = None
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    supplement_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    receipt: EvidenceReceipt

    @model_validator(mode="after")
    def analytics_receipts_match(self) -> "HistoricalAnalyticsEvidence":
        manifest = _revalidate(self.input_manifest, EventAnalyticsInputManifest)
        report = _revalidate(self.event_study, EventStudyReport)
        expected = _model_sha256(manifest)
        report_sha = event_study_report_sha256(report)
        supplement = _revalidate(self.supplement, EventStudySupplement) if self.supplement is not None else None
        supplement_sha = _model_sha256(supplement) if supplement is not None else None
        if self.manifest_sha256 != expected or self.report_sha256 != report_sha or self.supplement_sha256 != supplement_sha:
            raise ValueError("analytics component checksums do not match validated artifacts")
        combined = sha256_hex({"manifest": expected, "report": report_sha, "supplement": supplement_sha})
        if self.receipt.content_sha256 != combined:
            raise ValueError("analytics receipt must bind manifest, report, and supplement")
        manifest_ids = manifest.event_ids
        observation_ids = tuple(item.event_id for item in report.observations)
        if (
            report.event_count != len(report.observations)
            or len(observation_ids) != len(set(observation_ids))
            or observation_ids != manifest_ids
        ):
            raise ValueError("event-study observations must uniquely match manifest event IDs in order")
        if report.comparable_count != sum(item.comparable for item in report.observations):
            raise ValueError("event-study comparable_count must match observations")
        # The provider manifest and event study intentionally use different
        # input fingerprints.  Their supported binding is the exact ordered
        # event-ID set above, plus shared ticker validation at bundle assembly;
        # neither checksum is relabeled as the other.
        if supplement is not None:
            if supplement.base_report_sha256 != event_study_report_sha256(report):
                raise ValueError("analytics supplement must be bound to the embedded event study")
        if self.receipt.component != "historical_analytics":
            raise ValueError("analytics receipt component is invalid")
        eligibility = manifest.eligibility.value
        expected_quality = "complete" if eligibility == "eligible" else "degraded"
        if eligibility == "blocked" or self.receipt.quality != expected_quality:
            raise ValueError("historical analytics receipt quality must match manifest eligibility")
        return self


class MissingnessKind(StrEnum):
    NOT_PROVIDED = "not_provided"
    INSUFFICIENT_HISTORY = "insufficient_history"
    RIGHTS_RESTRICTED = "rights_restricted"
    CUTOFF_EXCLUDED = "cutoff_excluded"
    INCOMPATIBLE_UNIT = "incompatible_unit"
    PROVIDER_DEGRADED = "provider_degraded"
    NOT_COMPUTED = "not_computed"


class EvidenceMissingness(_Frozen):
    component: str = Field(min_length=1, max_length=80)
    field: str = Field(min_length=1, max_length=160)
    kind: MissingnessKind
    reason: str = Field(min_length=1, max_length=1_000)
    receipt_id: str | None = None


class NewlyCoveredIssuerEligibility(_Frozen):
    status: Literal["newly_covered"] = "newly_covered"
    ticker: str = Field(min_length=1, max_length=16)
    approved_by: str = Field(min_length=1, max_length=200)
    approved_at: datetime
    reason: str = Field(min_length=1, max_length=1_000)
    source_ref: str = Field(min_length=1, max_length=1_000)
    eligibility_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    @field_validator("ticker")
    @classmethod
    def ticker_upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("approved_at")
    @classmethod
    def approved_at_utc(cls, value: datetime) -> datetime:
        return _utc(value, "newly-covered issuer approved_at")

    @model_validator(mode="after")
    def checksum_matches(self) -> "NewlyCoveredIssuerEligibility":
        expected = sha256_hex(self.model_dump(mode="json", exclude={"eligibility_sha256"}))
        if self.eligibility_sha256 is None:
            object.__setattr__(self, "eligibility_sha256", expected)
        elif self.eligibility_sha256 != expected:
            raise ValueError("newly-covered issuer eligibility checksum does not match")
        return self


_USD_SCALE_UNITS = {MetricUnit.USD, MetricUnit.USD_MILLION, MetricUnit.USD_BILLION}


def _financial_key(value: MetricValue) -> tuple[object, ...]:
    """The comparison identity deliberately excludes only unit scale."""

    period = value.fiscal_period
    return (
        value.metric, period.fiscal_year, period.fiscal_quarter, period.period_end,
        value.accounting_basis, value.growth_basis, value.scope,
    )


def _validate_comparable_financial_units(
    consensus: ConsensusEvidence | None, management: ManagementEvidence | None,
) -> None:
    """Fail closed on incompatible units for otherwise identical facts.

    This is transport validation, not consensus/guidance reconciliation: USD,
    USD-million, and USD-billion can be scale-converted later; percent, ratio,
    percentage points, EPS, and counts are never silently comparable.
    """

    units: dict[tuple[object, ...], MetricUnit] = {}

    def add(value: MetricValue) -> None:
        key = _financial_key(value)
        previous = units.get(key)
        if previous is not None and previous != value.unit and not ({previous, value.unit} <= _USD_SCALE_UNITS):
            raise ValueError("comparable financial facts have incompatible units")
        units[key] = previous or value.unit

    if consensus is not None:
        for value in consensus.consensus.metrics.values():
            add(value)
    if management is not None:
        for entry in management.guidance:
            guidance = entry.guidance
            if guidance.type.value == "qualitative":
                continue
            add(MetricValue(
                metric=entry.metric, value=guidance.midpoint, unit=guidance.unit,
                fiscal_period=entry.target_fiscal_period,
                accounting_basis=guidance.accounting_basis, growth_basis=guidance.growth_basis,
                scope=guidance.scope,
            ))


def _validate_receipt_dag(receipts: list[EvidenceReceipt]) -> None:
    by_id = {item.receipt_id: item for item in receipts}
    state: dict[str, int] = {}

    def visit(receipt_id: str) -> None:
        mark = state.get(receipt_id, 0)
        if mark == 1:
            raise ValueError("evidence receipt input provenance cannot contain a cycle")
        if mark == 2:
            return
        state[receipt_id] = 1
        for parent in by_id[receipt_id].input_receipt_ids:
            visit(parent)
        state[receipt_id] = 2

    for receipt in receipts:
        visit(receipt.receipt_id)


def _validate_field_missingness(bundle: "ResearchEvidenceBundle", context: CompanyContextArtifact) -> None:
    """Resolve every field marker against the embedded, typed component.

    ``__component__`` is reserved for an absent optional section.  The only
    field-level surface currently transported by MVP-401 is consensus: a
    period or a named canonical metric.  This deliberately small allowlist
    prevents free-form caveats from masquerading as resolved missingness.
    """

    del context  # kept in the signature to make this resolution boundary explicit.
    present = {
        "prior_cards": bool(bundle.prior_cards), "consensus": bundle.consensus is not None,
        "management": bundle.management is not None, "news": bundle.news is not None,
        "technical": bundle.technical is not None,
        "historical_analytics": bundle.historical_analytics is not None,
    }
    for item in bundle.missingness:
        if item.component not in present:
            raise ValueError("missingness component is not allowlisted")
        if item.field == "__component__":
            if present[item.component] and not (
                item.component == "prior_cards"
                and item.kind is MissingnessKind.INSUFFICIENT_HISTORY
                and len(bundle.prior_cards) < 3
            ):
                raise ValueError("present optional component cannot have component missingness")
            continue
        if not present[item.component]:
            raise ValueError("absent optional component may only use __component__ missingness")
        if item.component != "consensus":
            raise ValueError("field-level missingness is not allowlisted for this component")
        consensus = bundle.consensus
        assert consensus is not None
        if item.field == "fiscal_period":
            if consensus.consensus.fiscal_period is not None:
                raise ValueError("missingness marks present consensus fiscal_period")
            continue
        if not item.field.startswith("metrics.") or len(item.field) == len("metrics."):
            raise ValueError("consensus missingness field is not allowlisted")
        metric_id = item.field.removeprefix("metrics.")
        metric = next((
            value for key, value in consensus.consensus.metrics.items()
            if key.value == metric_id
        ), None)
        if metric is not None and metric.value is not None:
            raise ValueError("missingness marks a present consensus metric")


class ResearchEvidenceBundle(_Frozen):
    schema_version: Literal[RESEARCH_EVIDENCE_SCHEMA_VERSION] = RESEARCH_EVIDENCE_SCHEMA_VERSION
    bundle_id: str = Field(min_length=1, max_length=200)
    target: TargetEventIdentity
    information_cutoff_at: datetime
    assembled_at: datetime
    company_context: CompanyContextEvidence
    ontology_coverage: OntologyCoverage
    prior_cards: tuple[PriorContextCardEvidence, ...] = ()
    consensus: ConsensusEvidence | None = None
    management: ManagementEvidence | None = None
    news: NewsEvidence | None = None
    technical: TechnicalEvidence | None = None
    historical_analytics: HistoricalAnalyticsEvidence | None = None
    newly_covered_issuer: NewlyCoveredIssuerEligibility | None = None
    missingness: tuple[EvidenceMissingness, ...] = ()
    bundle_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("information_cutoff_at", "assembled_at")
    @classmethod
    def timestamps_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def immutable_facts_are_coherent(self) -> "ResearchEvidenceBundle":
        cutoff = self.information_cutoff_at
        target = _revalidate(self.target, TargetEventIdentity)
        if self.assembled_at < cutoff:
            raise ValueError("assembled_at cannot precede information_cutoff_at")
        if target.event_at is not None:
            if target.event_at <= cutoff:
                raise ValueError("target event must be strictly after information cutoff")
        elif target.event_date <= cutoff.date():
            raise ValueError("target with unknown release time must be after information cutoff date")
        context = _revalidate(self.company_context.context, CompanyContextArtifact)
        if context.ticker != target.ticker or context.information_cutoff_at != cutoff:
            raise ValueError("company context ticker and cutoff must match the target bundle")
        expected_coverage = OntologyCoverage.from_context(context)
        if self.ontology_coverage != expected_coverage:
            raise ValueError("ontology coverage must be derived exactly from company context")
        if any(source.event_id == target.event_id for source in context.sources):
            raise ValueError("company-context sources cannot contain the target event")
        prior_ids = tuple(item.prior_event_id for item in self.prior_cards)
        if prior_ids != tuple(sorted(prior_ids)) or len(prior_ids) != len(set(prior_ids)):
            raise ValueError("prior cards must be sorted and unique by prior_event_id")
        card_ids = tuple(item.card_id for item in self.prior_cards)
        card_hashes = tuple(item.card_sha256 for item in self.prior_cards)
        if len(card_ids) != len(set(card_ids)) or len(card_hashes) != len(set(card_hashes)):
            raise ValueError("prior cards must have unique stable card IDs and checksums")
        for prior in self.prior_cards:
            card = _revalidate(prior.card, ContextCardV4)
            if card.identity.card.status != "approved":
                raise ValueError("prior context cards must be approved")
            if prior.prior_event_id == target.event_id:
                raise ValueError("prior cards cannot contain the target event")
            if card.identity.ticker != target.ticker:
                raise ValueError("prior-card ticker must match target ticker")
            if prior.event_date >= target.event_date:
                raise ValueError("prior-card event date must precede the target event date")
            if prior.knowledge_cutoff_at >= cutoff or prior.receipt.available_at >= cutoff:
                raise ValueError("prior cards must be strictly available before cutoff")
        receipts = [self.company_context.receipt, *(item.receipt for item in self.prior_cards)]
        for component in (self.consensus, self.management, self.news, self.technical, self.historical_analytics):
            if component is not None:
                receipts.append(component.receipt)
        ids = tuple(item.receipt_id for item in receipts)
        if len(ids) != len(set(ids)):
            raise ValueError("evidence receipt IDs must be globally unique")
        if any(item.available_at >= cutoff for item in receipts):
            raise ValueError("evidence receipts must be strictly before the information cutoff")
        receipt_by_id = {item.receipt_id: item for item in receipts}
        for receipt in receipts:
            if receipt.receipt_id in receipt.input_receipt_ids:
                raise ValueError("evidence receipt cannot list itself as an input")
            unknown_inputs = set(receipt.input_receipt_ids) - receipt_by_id.keys()
            if unknown_inputs:
                raise ValueError("evidence receipt has dangling input provenance")
        _validate_receipt_dag(receipts)
        if self.management is not None and self.management.statements != context.management:
            raise ValueError("management statements must exactly match company context management")
        if self.management is not None:
            management_refs = {
                reference for entry in self.management.guidance
                for reference in (*entry.evidence_refs, *entry.actual_evidence_refs)
            }
            known_management_refs = {
                self.management.receipt.source_ref, *self.management.receipt.provenance_refs,
            }
            if not management_refs.issubset(known_management_refs):
                raise ValueError("management guidance has dangling evidence refs")
            if target.event_id in management_refs or any(
                entry.guidance_id == target.event_id for entry in self.management.guidance
            ):
                raise ValueError("management guidance cannot contain the target event")
        if self.consensus is not None and self.consensus.consensus.fiscal_period not in {None, target.fiscal_period}:
            raise ValueError("consensus fiscal period must match target fiscal period")
        if self.consensus is not None:
            known_consensus_refs = {
                self.consensus.receipt.source_ref, *self.consensus.receipt.provenance_refs,
            }
            if not set(self.consensus.evidence_refs).issubset(known_consensus_refs):
                raise ValueError("consensus has dangling evidence refs")
            if target.event_id in self.consensus.evidence_refs:
                raise ValueError("consensus cannot contain the target event")
            if self.consensus.availability_status in {"missing", "partial", "conflicted"} and not any(
                item.component == "consensus" and item.field != "__component__"
                for item in self.missingness
            ):
                raise ValueError("partial, conflicted, or missing consensus requires field missingness")
        _validate_comparable_financial_units(self.consensus, self.management)
        if self.news is not None:
            signal = _revalidate(self.news.signal, NewsSignal)
            wrong_ticker = any(item.request.ticker != target.ticker for item in signal.classifications)
            if signal.cutoff_at != cutoff or wrong_ticker:
                raise ValueError("news signal cutoff or classification ticker mismatches target bundle")
        if self.technical is not None:
            technical = self.technical.technical
            if (technical.ticker.upper(), technical.event_id, technical.event_date, technical.cutoff_at) != (
                target.ticker, target.event_id, target.event_date, cutoff,
            ):
                raise ValueError("technical projection identity, event date, and cutoff must match target")
        if self.historical_analytics is not None:
            analytics = self.historical_analytics
            manifest = _revalidate(analytics.input_manifest, EventAnalyticsInputManifest)
            report = _revalidate(analytics.event_study, EventStudyReport)
            if manifest.ticker.upper() != target.ticker or report.ticker.upper() != target.ticker:
                raise ValueError("historical analytics ticker must match target")
            if (
                manifest.market != target.market
                or manifest.exchange_calendar != target.exchange_calendar
                or manifest.currency != target.currency
            ):
                raise ValueError("historical analytics market, calendar, and currency must match target")
            if manifest.information_cutoff_at >= cutoff:
                raise ValueError("historical analytics manifest must be strictly before bundle cutoff")
            if target.event_id in manifest.event_ids or any(
                item.event_id == target.event_id
                or item.event_date >= target.event_date
                or item.information_cutoff_at >= cutoff
                for item in report.observations
            ):
                raise ValueError("historical analytics cannot include target or future event outcomes")
        history_markers = tuple(
            item for item in self.missingness
            if item.component == "prior_cards"
            and item.kind is MissingnessKind.INSUFFICIENT_HISTORY
        )
        if self.newly_covered_issuer is None and history_markers:
            raise ValueError(
                "insufficient_history requires explicit newly-covered issuer eligibility"
            )
        if self.newly_covered_issuer is not None:
            if len(self.prior_cards) >= 3:
                raise ValueError("newly-covered issuer eligibility is invalid with three prior cards")
            if len(history_markers) != 1:
                raise ValueError(
                    "newly-covered issuer eligibility requires one insufficient_history marker"
                )
            if self.newly_covered_issuer.ticker != target.ticker:
                raise ValueError("newly-covered issuer eligibility ticker must match target")
            if self.newly_covered_issuer.approved_at >= cutoff:
                raise ValueError("newly-covered issuer approval must precede information cutoff")
        missing_keys = tuple((item.component, item.field) for item in self.missingness)
        if missing_keys != tuple(sorted(missing_keys)) or len(missing_keys) != len(set(missing_keys)):
            raise ValueError("missingness entries must be sorted and unique by component and field")
        if any(item.receipt_id is not None and item.receipt_id not in ids for item in self.missingness):
            raise ValueError("missingness receipt_id must reference an embedded receipt")
        optional = {
            "prior_cards": bool(self.prior_cards), "consensus": self.consensus is not None,
            "management": self.management is not None, "news": self.news is not None,
            "technical": self.technical is not None, "historical_analytics": self.historical_analytics is not None,
        }
        missing_components = {
            item.component for item in self.missingness if item.field == "__component__"
        }
        for component, present in optional.items():
            if present and component in missing_components:
                allowed_partial_history = (
                    component == "prior_cards"
                    and len(self.prior_cards) < 3
                    and any(
                        item.component == "prior_cards"
                        and item.field == "__component__"
                        and item.kind is MissingnessKind.INSUFFICIENT_HISTORY
                        for item in self.missingness
                    )
                )
                if not allowed_partial_history:
                    raise ValueError("present optional component cannot be marked missing")
            if not present and component not in missing_components:
                raise ValueError("absent optional component requires typed component missingness")
        _validate_field_missingness(self, context)
        expected = self.content_checksum()
        if self.bundle_sha256 is None:
            object.__setattr__(self, "bundle_sha256", expected)
        elif self.bundle_sha256 != expected:
            raise ValueError("bundle_sha256 does not match research evidence bundle")
        return self

    def content_checksum(self) -> str:
        return sha256_hex(self.model_dump(mode="json", exclude={"bundle_sha256"}))

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


def assemble_research_evidence_bundle(
    *, bundle_id: str, target: TargetEventIdentity, information_cutoff_at: datetime,
    assembled_at: datetime, company_context: CompanyContextEvidence,
    prior_cards: tuple[PriorContextCardEvidence, ...] = (),
    consensus: ConsensusEvidence | None = None, management: ManagementEvidence | None = None,
    news: NewsEvidence | None = None, technical: TechnicalEvidence | None = None,
    historical_analytics: HistoricalAnalyticsEvidence | None = None,
    newly_covered_issuer: NewlyCoveredIssuerEligibility | None = None,
    missingness: tuple[EvidenceMissingness, ...] = (),
) -> ResearchEvidenceBundle:
    """Pure deterministic assembly of the canonical synthesis input."""

    context = _revalidate(company_context.context, CompanyContextArtifact)
    supplied_missing = {(item.component, item.field): item for item in missingness}
    optional = {
        "prior_cards": bool(prior_cards), "consensus": consensus is not None,
        "management": management is not None, "news": news is not None,
        "technical": technical is not None, "historical_analytics": historical_analytics is not None,
    }
    for component, present in optional.items():
        if not present and (component, "__component__") not in supplied_missing:
            supplied_missing[(component, "__component__")] = EvidenceMissingness(
                component=component, field="__component__", kind=MissingnessKind.NOT_PROVIDED,
                reason=f"{component} was not supplied to the research evidence bundle",
            )
    return ResearchEvidenceBundle(
        bundle_id=bundle_id,
        target=target,
        information_cutoff_at=information_cutoff_at,
        assembled_at=assembled_at,
        company_context=company_context,
        ontology_coverage=OntologyCoverage.from_context(context),
        prior_cards=tuple(sorted(prior_cards, key=lambda item: item.prior_event_id)),
        consensus=consensus,
        management=management,
        news=news,
        technical=technical,
        historical_analytics=historical_analytics,
        newly_covered_issuer=newly_covered_issuer,
        missingness=tuple(sorted(supplied_missing.values(), key=lambda item: (item.component, item.field))),
    )


class ResearchEvidenceStore:
    """Optional canonical on-disk persistence for a validated evidence bundle."""

    def write(self, bundle: ResearchEvidenceBundle, target: Path) -> str:
        payload = bundle.canonical_bytes() + b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return digest

    def read(self, target: Path) -> ResearchEvidenceBundle:
        payload = target.read_bytes()
        bundle = ResearchEvidenceBundle.model_validate_json(payload)
        if payload != bundle.canonical_bytes() + b"\n":
            raise ValueError("research-evidence bytes are not canonical")
        return bundle

    def read_legacy_v1_payload(self, target: Path) -> dict[str, object]:
        """Verify and expose legacy v1 bytes without authorizing synthesis."""

        payload = target.read_bytes()
        def reject_duplicate_keys(pairs):
            document = {}
            for key, value in pairs:
                if key in document:
                    raise ValueError(f"legacy v1 payload repeats JSON key: {key}")
                document[key] = value
            return document

        document = json.loads(payload, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(document, dict) or document.get("schema_version") != "eventlens.research-evidence.v1":
            raise ValueError("legacy reader requires eventlens.research-evidence.v1")
        if "newly_covered_issuer" in document:
            raise ValueError("legacy v1 payload contains a post-v1 field")
        checksum = document.get("bundle_sha256")
        expected = sha256_hex({
            key: value for key, value in document.items() if key != "bundle_sha256"
        })
        if checksum != expected:
            raise ValueError("legacy v1 bundle checksum does not match")
        if payload != canonical_json_bytes(document) + b"\n":
            raise ValueError("legacy v1 research-evidence bytes are not canonical")
        return document


def research_evidence_json_schema() -> dict[str, object]:
    """Return the validation schema for the immutable synthesis input."""

    return ResearchEvidenceBundle.model_json_schema()


__all__ = [
    "RESEARCH_EVIDENCE_SCHEMA_VERSION", "CompanyContextEvidence", "ConsensusEvidence",
    "EvidenceMissingness", "EvidenceReceipt", "HistoricalAnalyticsEvidence", "ManagementEvidence",
    "MissingnessKind", "NewlyCoveredIssuerEligibility", "NewsEvidence", "OntologyCoverage", "PriorContextCardEvidence",
    "ResearchEvidenceBundle", "ResearchEvidenceStore", "TargetEventIdentity", "TechnicalEvidence",
    "TechnicalProjection", "assemble_research_evidence_bundle", "research_evidence_json_schema",
]
