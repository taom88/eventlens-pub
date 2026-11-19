"""SQLite repository implementing the hosted research-memory contract locally."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel

from eventlens.persistence.memory_models import (
    CompanyRecord,
    ContextCardStatus,
    EarningsEventRecord,
    EventContextCardRecord,
    EventMetricRecord,
    HypothesisAssessmentRecord,
    HypothesisRecord,
    HypothesisStatus,
    MarketReactionRecord,
    MarketSetupRecord,
    PreEventSnapshotRecord,
    ResearchSessionRecord,
    SourceRecord,
    SourcePassageRecord,
    VersionStatus,
)
from .models import EvidenceLink, EventMemoryBundle, EventSourceLink
from .protocols import ImmutableRevisionError, MemoryRepositoryError
from eventlens.persistence.run_records import DurableRunRecord, RunRecordKind


RecordT = TypeVar("RecordT", bound=BaseModel)
TABLE_MODELS: dict[str, type[BaseModel]] = {
    "companies": CompanyRecord,
    "sources": SourceRecord,
    "source_passages": SourcePassageRecord,
    "earnings_events": EarningsEventRecord,
    "event_metrics": EventMetricRecord,
    "market_setups": MarketSetupRecord,
    "market_reactions": MarketReactionRecord,
    "research_sessions": ResearchSessionRecord,
    "hypotheses": HypothesisRecord,
    "pre_event_snapshots": PreEventSnapshotRecord,
    "hypothesis_assessments": HypothesisAssessmentRecord,
    "event_context_cards": EventContextCardRecord,
}
APPEND_ONLY = frozenset(
    {"sources", "source_passages", "event_metrics", "market_setups", "market_reactions"}
)


class SQLiteResearchMemoryRepository:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def save_company(self, record: CompanyRecord) -> CompanyRecord:
        return self._save("companies", record)

    def save_run_record(self, record: DurableRunRecord) -> DurableRunRecord:
        payload = record.model_dump_json()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM durable_run_records WHERE id=?", (str(record.id),)
            ).fetchone()
            if existing is not None:
                if DurableRunRecord.model_validate_json(existing[0]) == record:
                    return record
                raise ImmutableRevisionError("durable run record revision is immutable")
            scoped = connection.execute(
                "SELECT owner_id,workspace_id,session_id FROM durable_run_records WHERE run_id=? LIMIT 1",
                (str(record.run_id),),
            ).fetchone()
            if scoped is not None and tuple(scoped) != (
                record.owner_id, record.workspace_id, str(record.session_id)
            ):
                raise MemoryRepositoryError("run ownership or workspace scope mismatch")
            duplicate = connection.execute(
                "SELECT id FROM durable_run_records WHERE workspace_id=? AND run_id=? AND kind=? AND revision=?",
                (record.workspace_id, str(record.run_id), record.kind.value, record.revision),
            ).fetchone()
            if duplicate is not None:
                raise ImmutableRevisionError("durable run record revision already exists")
            connection.execute(
                "INSERT INTO durable_run_records(id,owner_id,workspace_id,run_id,session_id,kind,revision,created_at,checksum,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(record.id), record.owner_id, record.workspace_id, str(record.run_id),
                 str(record.session_id), record.kind.value, record.revision,
                 record.created_at.isoformat(), record.checksum, payload),
            )
        return record

    def get_run_record(self, workspace_id: str, run_id: UUID, kind: RunRecordKind, revision: int) -> DurableRunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM durable_run_records WHERE workspace_id=? AND run_id=? AND kind=? AND revision=?",
                (workspace_id, str(run_id), kind.value, revision),
            ).fetchone()
        return DurableRunRecord.model_validate_json(row[0]) if row else None

    def list_run_records(self, workspace_id: str, run_id: UUID, *, kind: RunRecordKind | None = None) -> tuple[DurableRunRecord, ...]:
        query = "SELECT payload_json FROM durable_run_records WHERE workspace_id=? AND run_id=?"
        params: list[str] = [workspace_id, str(run_id)]
        if kind is not None:
            query += " AND kind=?"
            params.append(kind.value)
        query += " ORDER BY kind,revision"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(DurableRunRecord.model_validate_json(row[0]) for row in rows)

    def save_source(self, record: SourceRecord) -> SourceRecord:
        return self._save("sources", record)

    def save_source_passage(self, record: SourcePassageRecord) -> SourcePassageRecord:
        return self._save("source_passages", record)

    def save_event(
        self, record: EarningsEventRecord, *, source_links: tuple[EventSourceLink, ...] = ()
    ) -> EarningsEventRecord:
        result = self._save("earnings_events", record)
        self._save_links("event_source", record.id, source_links)
        return result

    def save_metric(self, record: EventMetricRecord) -> EventMetricRecord:
        return self._save("event_metrics", record)

    def save_market_setup(
        self, record: MarketSetupRecord, *, source_ids: tuple[UUID, ...] = ()
    ) -> MarketSetupRecord:
        result = self._save("market_setups", record)
        self._save_id_links("market_setup_source", record.id, source_ids)
        return result

    def save_market_reaction(
        self, record: MarketReactionRecord, *, source_ids: tuple[UUID, ...] = ()
    ) -> MarketReactionRecord:
        result = self._save("market_reactions", record)
        self._save_id_links("market_reaction_source", record.id, source_ids)
        return result

    def save_session(self, record: ResearchSessionRecord) -> ResearchSessionRecord:
        return self._save("research_sessions", record)

    def save_hypothesis(self, record: HypothesisRecord) -> HypothesisRecord:
        self._require_draft_first("hypotheses", record)
        return self._save("hypotheses", record)

    def save_snapshot(
        self,
        record: PreEventSnapshotRecord,
        *,
        evidence_links: tuple[EvidenceLink, ...] = (),
        metric_ids: tuple[UUID, ...] = (),
        replace_links: bool = False,
    ) -> PreEventSnapshotRecord:
        self._require_draft_first("pre_event_snapshots", record)
        if record.status is VersionStatus.DRAFT:
            with self._connect() as connection:
                self._validate_snapshot_inputs(
                    connection, record, evidence_links, metric_ids
                )
        result = self._save("pre_event_snapshots", record)
        if record.status is VersionStatus.DRAFT:
            self._save_links(
                "snapshot_evidence",
                record.id,
                evidence_links,
                replace=replace_links,
            )
            self._save_id_links(
                "snapshot_metric",
                record.id,
                metric_ids,
                replace=replace_links,
            )
        return result

    def save_hypothesis_assessment(
        self,
        record: HypothesisAssessmentRecord,
        *,
        evidence_links: tuple[EvidenceLink, ...] = (),
        replace_links: bool = False,
    ) -> HypothesisAssessmentRecord:
        self._require_draft_first("hypothesis_assessments", record)
        if record.status is VersionStatus.DRAFT:
            with self._connect() as connection:
                self._validate_evidence_links(
                    connection,
                    evidence_links,
                    cutoff_at=record.knowledge_cutoff_at,
                    data_kind=getattr(
                        self._one(connection, "research_sessions", record.research_session_id),
                        "data_kind",
                        None,
                    ),
                )
        result = self._save("hypothesis_assessments", record)
        if record.status is VersionStatus.DRAFT:
            self._save_links(
                "assessment_evidence",
                record.id,
                evidence_links,
                replace=replace_links,
            )
        return result

    def save_context_card(
        self,
        record: EventContextCardRecord,
        *,
        evidence_links: tuple[EvidenceLink, ...] = (),
        replace_links: bool = False,
    ) -> EventContextCardRecord:
        self._require_draft_first("event_context_cards", record)
        if record.status is ContextCardStatus.DRAFT:
            with self._connect() as connection:
                self._validate_evidence_links(
                    connection,
                    evidence_links,
                    cutoff_at=record.knowledge_cutoff_at,
                    data_kind=record.data_kind,
                )
        result = self._save("event_context_cards", record)
        if record.status is ContextCardStatus.DRAFT:
            self._save_links(
                "card_evidence",
                record.id,
                evidence_links,
                replace=replace_links,
            )
        return result

    def load_session_bundle(self, session_id: UUID) -> EventMemoryBundle:
        with self._connect() as connection:
            session = self._one(connection, "research_sessions", session_id)
            if not isinstance(session, ResearchSessionRecord):
                raise MemoryRepositoryError("research session was not found")
            event = self._one(connection, "earnings_events", session.event_id)
            company = self._one(connection, "companies", session.company_id)
            if not isinstance(event, EarningsEventRecord) or not isinstance(
                company, CompanyRecord
            ):
                raise MemoryRepositoryError("research session has incomplete lineage")
            hypotheses = tuple(self._many(connection, "hypotheses", "research_session_id", session.id))
            assessments = tuple(
                item
                for hypothesis in hypotheses
                for item in self._many(connection, "hypothesis_assessments", "hypothesis_id", hypothesis.id)
            )
            event_links = tuple(self._load_links("event_source", event.id, EventSourceLink))
            sources = tuple(
                source
                for link in event_links
                if isinstance((source := self._one(connection, "sources", link.source_id)), SourceRecord)
            )
            source_passages = tuple(
                passage
                for source in sources
                for passage in self._many(connection, "source_passages", "source_id", source.id)
            )
            snapshots = tuple(self._many(connection, "pre_event_snapshots", "research_session_id", session.id))
            cards = tuple(self._many(connection, "event_context_cards", "research_session_id", session.id))
            return EventMemoryBundle(
                company=company,
                event=event,
                metrics=tuple(self._many(connection, "event_metrics", "event_id", event.id)),
                market_setup=_last(self._many(connection, "market_setups", "event_id", event.id)),
                market_reaction=_last(self._many(connection, "market_reactions", "event_id", event.id)),
                sources=sources,
                source_passages=source_passages,
                session=session,
                hypotheses=hypotheses,
                hypothesis_assessments=assessments,
                snapshots=snapshots,
                context_cards=cards,
                event_source_links=event_links,
                snapshot_evidence={item.id: tuple(self._load_links("snapshot_evidence", item.id, EvidenceLink)) for item in snapshots},
                assessment_evidence={item.id: tuple(self._load_links("assessment_evidence", item.id, EvidenceLink)) for item in assessments},
                context_card_evidence={item.id: tuple(self._load_links("card_evidence", item.id, EvidenceLink)) for item in cards},
            )

    def list_prior_context_cards(self, ticker: str, before_event_date, cutoff_at, *, limit: int = 12) -> tuple[EventContextCardRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            cards = []
            for row in connection.execute("SELECT payload_json FROM event_context_cards"):
                card = EventContextCardRecord.model_validate_json(row[0])
                if card.status is not ContextCardStatus.APPROVED or card.knowledge_cutoff_at > cutoff_at:
                    continue
                event = self._one(connection, "earnings_events", card.event_id)
                company = self._one(connection, "companies", event.company_id) if isinstance(event, EarningsEventRecord) else None
                if not isinstance(event, EarningsEventRecord) or not isinstance(company, CompanyRecord):
                    continue
                if company.ticker.upper() != ticker.upper() or event.event_date >= before_event_date:
                    continue
                cards.append(card.model_copy(update={
                    "ticker": company.ticker,
                    "event_date": event.event_date,
                    "external_event_id": event.external_event_id,
                }))
        newest_by_event = {}
        for card in cards:
            event_key = card.external_event_id or str(card.event_id)
            current = newest_by_event.get(event_key)
            if current is None or (card.version, card.knowledge_cutoff_at, str(card.id)) > (
                current.version, current.knowledge_cutoff_at, str(current.id)
            ):
                newest_by_event[event_key] = card
        selected = sorted(
            newest_by_event.values(),
            key=lambda item: (item.knowledge_cutoff_at, item.event_date or date.min, str(item.id)),
            reverse=True,
        )
        return tuple(selected[:limit])

    def _initialize(self) -> None:
        with self._connect() as connection:
            for table in TABLE_MODELS:
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS memory_links (
                    kind TEXT NOT NULL, owner_id TEXT NOT NULL, target_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(kind, owner_id, target_key)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS durable_run_records (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0), created_at TEXT NOT NULL,
                    checksum TEXT NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(workspace_id, run_id, kind, revision)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _save(self, table: str, record: RecordT) -> RecordT:
        with self._connect() as connection:
            self._validate_dependencies(connection, record)
            existing = self._one(connection, table, record.id)
            if existing is not None:
                if table in APPEND_ONLY:
                    if existing == record:
                        return record
                    raise ImmutableRevisionError(f"{table} revision is immutable")
                if _requires_frozen_content(record) and not _transition_preserves_content(existing, record):
                    raise ImmutableRevisionError(f"{table} finalization changed reviewed content")
                if _terminal(existing) and existing != record and not _allowed_terminal_transition(existing, record):
                    raise ImmutableRevisionError(f"{table} revision is immutable")
            connection.execute(
                f"INSERT INTO {table}(id,payload_json) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json",
                (str(record.id), record.model_dump_json()),
            )
        return record

    def _validate_dependencies(self, connection: sqlite3.Connection, record: BaseModel) -> None:
        def require(table: str, key: UUID) -> BaseModel:
            found = self._one(connection, table, key)
            if found is None:
                raise MemoryRepositoryError(f"{type(record).__name__} references missing {table}")
            return found
        if isinstance(record, EarningsEventRecord):
            require("companies", record.company_id)
        elif isinstance(record, SourcePassageRecord):
            require("sources", record.source_id)
            if record.text_sha256 != _sha256_text(record.text):
                raise MemoryRepositoryError("source passage checksum does not match text")
        elif isinstance(record, (EventMetricRecord, MarketSetupRecord, MarketReactionRecord)):
            require("earnings_events", record.event_id)
        elif isinstance(record, ResearchSessionRecord):
            event = require("earnings_events", record.event_id)
            require("companies", record.company_id)
            if getattr(event, "company_id") != record.company_id or getattr(event, "data_kind") != record.data_kind:
                raise MemoryRepositoryError("session is inconsistent with event")
        elif isinstance(record, HypothesisRecord):
            session = require("research_sessions", record.research_session_id)
            if record.evidence_cutoff_at != getattr(session, "information_cutoff_at"):
                raise MemoryRepositoryError("hypothesis cutoff differs from session")
        elif isinstance(record, PreEventSnapshotRecord):
            session = require("research_sessions", record.research_session_id)
            if any((getattr(session, "event_id") != record.event_id, getattr(session, "run_mode") != record.run_mode, getattr(session, "data_kind") != record.data_kind, getattr(session, "information_cutoff_at") != record.information_cutoff_at)):
                raise MemoryRepositoryError("snapshot is inconsistent with session")
            if record.status is VersionStatus.FINALIZED:
                evidence = self._load_links_from_connection(
                    connection, "snapshot_evidence", record.id, EvidenceLink
                )
                metrics = self._load_links_from_connection(
                    connection, "snapshot_metric", record.id, _IdLink
                )
                self._validate_snapshot_inputs(
                    connection,
                    record,
                    tuple(evidence),
                    tuple(item.target_id for item in metrics),
                    require_nonempty=True,
                )
        elif isinstance(record, HypothesisAssessmentRecord):
            hypothesis = require("hypotheses", record.hypothesis_id)
            snapshot = require("pre_event_snapshots", record.snapshot_id)
            if record.research_session_id != getattr(hypothesis, "research_session_id") or record.research_session_id != getattr(snapshot, "research_session_id"):
                raise MemoryRepositoryError("assessment crosses sessions")
            session = require("research_sessions", record.research_session_id)
            if record.run_mode != getattr(session, "run_mode") or record.knowledge_cutoff_at != getattr(snapshot, "information_cutoff_at"):
                raise MemoryRepositoryError("assessment is inconsistent with session cutoff")
            if (
                getattr(hypothesis, "historical_context_manifest_sha256", None)
                != getattr(snapshot, "historical_context_manifest_sha256", None)
                or getattr(hypothesis, "historical_context_manifest", ())
                != getattr(snapshot, "historical_context_manifest", ())
            ):
                raise MemoryRepositoryError("hypothesis and snapshot historical context lineage differs")
            if record.status is VersionStatus.FINALIZED and (
                getattr(hypothesis, "status") is not HypothesisStatus.LOCKED
                or getattr(snapshot, "status") is not VersionStatus.FINALIZED
            ):
                raise MemoryRepositoryError("finalized assessment requires locked hypothesis and finalized snapshot")
            if record.status is VersionStatus.FINALIZED:
                evidence = self._load_links_from_connection(
                    connection, "assessment_evidence", record.id, EvidenceLink
                )
                if not evidence:
                    raise MemoryRepositoryError("finalized assessment requires persisted evidence")
                self._validate_evidence_links(
                    connection,
                    tuple(evidence),
                    cutoff_at=record.knowledge_cutoff_at,
                    data_kind=getattr(session, "data_kind"),
                )
        elif isinstance(record, EventContextCardRecord):
            session = require("research_sessions", record.research_session_id)
            refs = (
                require("pre_event_snapshots", record.snapshot_id),
                require("hypotheses", record.hypothesis_id),
                require("hypothesis_assessments", record.hypothesis_assessment_id),
                require("market_setups", record.market_setup_id),
                require("market_reactions", record.market_reaction_id),
            )
            if getattr(session, "event_id") != record.event_id or any(
                getattr(item, "event_id", record.event_id) != record.event_id for item in refs
            ):
                raise MemoryRepositoryError("context card crosses events")
            if record.run_mode != getattr(session, "run_mode") or record.data_kind != getattr(session, "data_kind"):
                raise MemoryRepositoryError("context card is inconsistent with session")
            for item in refs[:2]:
                if (
                    getattr(item, "historical_context_manifest_sha256", None)
                    != record.historical_context_manifest_sha256
                    or getattr(item, "historical_context_manifest", ())
                    != record.historical_context_manifest
                ):
                    raise MemoryRepositoryError("context card historical context lineage differs from its inputs")
            if record.status is ContextCardStatus.APPROVED and (
                getattr(refs[0], "status") is not VersionStatus.FINALIZED
                or getattr(refs[1], "status") is not HypothesisStatus.LOCKED
                or getattr(refs[2], "status") is not VersionStatus.FINALIZED
            ):
                raise MemoryRepositoryError("approved context card requires finalized independent objects")
            if record.status is ContextCardStatus.APPROVED:
                evidence = self._load_links_from_connection(
                    connection, "card_evidence", record.id, EvidenceLink
                )
                if not evidence:
                    raise MemoryRepositoryError("approved card requires persisted evidence")
                self._validate_evidence_links(
                    connection,
                    tuple(evidence),
                    cutoff_at=record.knowledge_cutoff_at,
                    data_kind=record.data_kind,
                )

    def _one(self, connection: sqlite3.Connection, table: str, key: UUID) -> BaseModel | None:
        row = connection.execute(f"SELECT payload_json FROM {table} WHERE id=?", (str(key),)).fetchone()
        return None if row is None else TABLE_MODELS[table].model_validate_json(row["payload_json"])

    def _many(self, connection: sqlite3.Connection, table: str, field: str, value: UUID) -> list[Any]:
        return sorted(
            [item for row in connection.execute(f"SELECT payload_json FROM {table}") if str(getattr((item := TABLE_MODELS[table].model_validate_json(row[0])), field)) == str(value)],
            key=lambda item: (getattr(item, "version", getattr(item, "setup_version", getattr(item, "reaction_version", 1))), str(item.id)),
        )

    def _require_draft_first(self, table: str, record: BaseModel) -> None:
        if not _requires_draft_first(record):
            return
        with self._connect() as connection:
            if self._one(connection, table, record.id) is None:
                raise MemoryRepositoryError(f"{table} terminal revision must first be saved as draft")

    def _save_id_links(
        self,
        kind: str,
        owner: UUID,
        targets: tuple[UUID, ...],
        *,
        replace: bool = False,
    ) -> None:
        self._save_links(
            kind,
            owner,
            tuple(_IdLink(target_id=item) for item in targets),
            replace=replace,
        )

    def _save_links(
        self,
        kind: str,
        owner: UUID,
        links: tuple[BaseModel, ...],
        *,
        replace: bool = False,
    ) -> None:
        with self._connect() as connection:
            if replace:
                connection.execute(
                    "DELETE FROM memory_links WHERE kind=? AND owner_id=?",
                    (kind, str(owner)),
                )
            for link in links:
                target = getattr(link, "source_id", getattr(link, "target_id", None))
                connection.execute(
                    "INSERT OR IGNORE INTO memory_links(kind,owner_id,target_key,payload_json) VALUES (?,?,?,?)",
                    (kind, str(owner), f"{target}:{getattr(link, 'passage_id', '')}:{getattr(link, 'relationship', '')}", link.model_dump_json()),
                )

    def _load_links(self, kind: str, owner: UUID, model: type[RecordT]) -> list[RecordT]:
        with self._connect() as connection:
            return self._load_links_from_connection(connection, kind, owner, model)

    def _load_links_from_connection(
        self,
        connection: sqlite3.Connection,
        kind: str,
        owner: UUID,
        model: type[RecordT],
    ) -> list[RecordT]:
        rows = connection.execute("SELECT payload_json FROM memory_links WHERE kind=? AND owner_id=? ORDER BY target_key", (kind, str(owner))).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def _validate_snapshot_inputs(
        self,
        connection: sqlite3.Connection,
        record: PreEventSnapshotRecord,
        evidence_links: tuple[EvidenceLink, ...],
        metric_ids: tuple[UUID, ...],
        *,
        require_nonempty: bool = False,
    ) -> None:
        if require_nonempty and not evidence_links:
            raise MemoryRepositoryError("finalized snapshot requires persisted evidence")
        if require_nonempty and not metric_ids:
            raise MemoryRepositoryError("finalized snapshot requires persisted metrics")
        self._validate_evidence_links(
            connection,
            evidence_links,
            cutoff_at=record.information_cutoff_at,
            data_kind=record.data_kind,
        )
        for metric_id in metric_ids:
            metric = self._one(connection, "event_metrics", metric_id)
            if not isinstance(metric, EventMetricRecord):
                raise MemoryRepositoryError("snapshot metric references missing metric")
            if metric.event_id != record.event_id:
                raise MemoryRepositoryError("snapshot metric crosses events")
            if metric.available_at > record.information_cutoff_at:
                raise MemoryRepositoryError("snapshot metric is unavailable at cutoff")
            if metric.data_kind != record.data_kind:
                raise MemoryRepositoryError("snapshot metric crosses data kinds")

    def _validate_evidence_links(
        self,
        connection: sqlite3.Connection,
        evidence_links: tuple[EvidenceLink, ...],
        *,
        cutoff_at: Any,
        data_kind: Any,
    ) -> None:
        for link in evidence_links:
            source = self._one(connection, "sources", link.source_id)
            if not isinstance(source, SourceRecord):
                raise MemoryRepositoryError("evidence references missing source")
            if source.available_at > cutoff_at:
                raise MemoryRepositoryError("evidence is unavailable at cutoff")
            if link.available_at is not None and link.available_at != source.available_at:
                raise MemoryRepositoryError("evidence link availability must match source")
            if data_kind is not None and source.data_kind != data_kind:
                raise MemoryRepositoryError("evidence crosses data kinds")


class _IdLink(BaseModel):
    target_id: UUID


def _terminal(record: BaseModel) -> bool:
    return (
        isinstance(record, HypothesisRecord) and record.status == HypothesisStatus.LOCKED
    ) or (
        isinstance(record, (PreEventSnapshotRecord, HypothesisAssessmentRecord)) and record.status == VersionStatus.FINALIZED
    ) or (
        isinstance(record, EventContextCardRecord)
        and record.status in {ContextCardStatus.REVIEWED, ContextCardStatus.APPROVED, ContextCardStatus.STALE}
    )


def _requires_draft_first(record: BaseModel) -> bool:
    return _terminal(record) or (
        isinstance(record, EventContextCardRecord) and record.status is ContextCardStatus.BLOCKED
    )


def _requires_frozen_content(record: BaseModel) -> bool:
    return _terminal(record)


def _allowed_terminal_transition(existing: BaseModel, incoming: BaseModel) -> bool:
    """Permit only content-identical v4 review/approval staleness transitions."""

    if not isinstance(existing, EventContextCardRecord) or not isinstance(incoming, EventContextCardRecord):
        return False
    if not _transition_preserves_content(existing, incoming):
        return False
    if existing.status is ContextCardStatus.REVIEWED:
        return incoming.status in {ContextCardStatus.APPROVED, ContextCardStatus.STALE}
    if existing.status is ContextCardStatus.APPROVED:
        return incoming.status is ContextCardStatus.STALE and incoming.schema_version.startswith("eventlens.context_card.v4")
    return False


def _transition_preserves_content(existing: BaseModel, incoming: BaseModel) -> bool:
    lifecycle: set[str] = set()
    if isinstance(existing, HypothesisRecord):
        lifecycle = {"status", "approved_at", "approved_by", "updated_at"}
    elif isinstance(existing, PreEventSnapshotRecord):
        lifecycle = {"status", "finalized_at", "approved_at", "approved_by", "updated_at"}
    elif isinstance(existing, HypothesisAssessmentRecord):
        lifecycle = {"status", "finalized_at", "updated_at"}
    elif isinstance(existing, EventContextCardRecord):
        lifecycle = {
            "status", "reviewed_at", "reviewed_by", "approved_at", "approved_by",
            "stale_at", "updated_at",
        }
    return existing.model_dump(mode="json", exclude=lifecycle) == incoming.model_dump(mode="json", exclude=lifecycle)


def _last(items: list[Any]) -> Any | None:
    return items[-1] if items else None


def _sha256_text(value: str) -> str:
    return __import__("hashlib").sha256(value.encode("utf-8")).hexdigest()
