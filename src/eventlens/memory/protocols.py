"""Repository contracts for durable research memory."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from .models import (
    CompanyRecord,
    EarningsEventRecord,
    EventContextCardRecord,
    EventMemoryBundle,
    EventMetricRecord,
    EventSourceLink,
    EvidenceLink,
    HypothesisAssessmentRecord,
    HypothesisRecord,
    MarketReactionRecord,
    MarketSetupRecord,
    PreEventSnapshotRecord,
    ResearchSessionRecord,
    SourceRecord,
    SourcePassageRecord,
)
from eventlens.persistence.run_records import DurableRunRecord, RunRecordKind


class MemoryRepositoryError(RuntimeError):
    """Safe public repository error; messages must never contain credentials."""


class ImmutableRevisionError(MemoryRepositoryError):
    """Raised when a finalized/approved revision would be overwritten."""


class ResearchMemoryRepository(Protocol):
    """Minimal persistence boundary required for Milestone 1."""

    def save_company(self, record: CompanyRecord) -> CompanyRecord: ...

    def save_event(
        self,
        record: EarningsEventRecord,
        *,
        source_links: tuple[EventSourceLink, ...] = (),
    ) -> EarningsEventRecord: ...

    def save_metric(self, record: EventMetricRecord) -> EventMetricRecord: ...

    def save_market_setup(
        self, record: MarketSetupRecord, *, source_ids: tuple[UUID, ...] = ()
    ) -> MarketSetupRecord: ...

    def save_market_reaction(
        self, record: MarketReactionRecord, *, source_ids: tuple[UUID, ...] = ()
    ) -> MarketReactionRecord: ...

    def save_source(self, record: SourceRecord) -> SourceRecord: ...

    def save_source_passage(self, record: SourcePassageRecord) -> SourcePassageRecord: ...

    def save_session(self, record: ResearchSessionRecord) -> ResearchSessionRecord: ...

    def save_hypothesis(self, record: HypothesisRecord) -> HypothesisRecord: ...

    def save_hypothesis_assessment(
        self,
        record: HypothesisAssessmentRecord,
        *,
        evidence_links: tuple[EvidenceLink, ...] = (),
        replace_links: bool = False,
    ) -> HypothesisAssessmentRecord: ...

    def save_snapshot(
        self,
        record: PreEventSnapshotRecord,
        *,
        evidence_links: tuple[EvidenceLink, ...] = (),
        metric_ids: tuple[UUID, ...] = (),
        replace_links: bool = False,
    ) -> PreEventSnapshotRecord: ...

    def save_context_card(
        self,
        record: EventContextCardRecord,
        *,
        evidence_links: tuple[EvidenceLink, ...] = (),
        replace_links: bool = False,
    ) -> EventContextCardRecord: ...

    def load_session_bundle(self, session_id: UUID) -> EventMemoryBundle: ...

    def list_prior_context_cards(
        self, ticker: str, before_event_date, cutoff_at, *, limit: int = 12
    ) -> tuple[EventContextCardRecord, ...]: ...

    def save_run_record(self, record: DurableRunRecord) -> DurableRunRecord: ...

    def get_run_record(
        self, workspace_id: str, run_id: UUID, kind: RunRecordKind, revision: int
    ) -> DurableRunRecord | None: ...

    def list_run_records(
        self, workspace_id: str, run_id: UUID, *, kind: RunRecordKind | None = None
    ) -> tuple[DurableRunRecord, ...]: ...
