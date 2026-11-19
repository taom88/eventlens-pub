"""Supabase/PostgREST adapter aligned to the checked-in Milestone 1 schema."""

from __future__ import annotations

import json
import hashlib
from datetime import date
from typing import Any, TypeVar
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, SecretStr

from eventlens.persistence.memory_models import (
    CompanyRecord,
    ContextCardStatus,
    EarningsEventRecord,
    EvidenceStorageSizeRecord,
    EventContextCardRecord,
    EventMetricRecord,
    HypothesisAssessmentRecord,
    HypothesisRecord,
    HypothesisStatus,
    MarketReactionRecord,
    MarketSetupRecord,
    PreEventSnapshotRecord,
    RelationSizeRecord,
    ResearchSessionRecord,
    SourceRecord,
    SourcePassageRecord,
    VersionStatus,
)
from .models import EvidenceLink, EventMemoryBundle, EventSourceLink
from .protocols import ImmutableRevisionError, MemoryRepositoryError
from .sqlite import (
    _allowed_terminal_transition,
    _requires_draft_first,
    _requires_frozen_content,
    _terminal,
    _transition_preserves_content,
)
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
APPEND_ONLY = frozenset({"sources", "source_passages", "event_metrics", "market_setups", "market_reactions"})


class SupabaseResearchMemoryRepository:
    def __init__(
        self,
        *,
        url: str,
        secret_key: SecretStr | str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        normalized = url.strip().rstrip("/")
        secret = (secret_key.get_secret_value() if isinstance(secret_key, SecretStr) else secret_key).strip()
        if not normalized or not secret:
            raise ValueError("Supabase URL and server secret key are required")
        self._base_url = f"{normalized}/rest/v1"
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._headers = {"apikey": secret, "Authorization": f"Bearer {secret}", "Content-Type": "application/json", "Accept": "application/json"}

    def __repr__(self) -> str:
        return "SupabaseResearchMemoryRepository(configured=True)"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def save_company(self, record: CompanyRecord) -> CompanyRecord:
        return self._save("companies", record)

    def save_run_record(self, record: DurableRunRecord) -> DurableRunRecord:
        existing = self.get_run_record(record.workspace_id, record.run_id, record.kind, record.revision)
        if existing is not None:
            if existing == record:
                return record
            raise ImmutableRevisionError("durable run record revision is immutable")
        rows = self._request("GET", "eventlens_run_records", params={
            "select": "owner_id,workspace_id,session_id", "run_id": f"eq.{record.run_id}",
            "limit": "1",
        })
        if rows and (rows[0]["owner_id"], rows[0]["workspace_id"], rows[0]["session_id"]) != (
            record.owner_id, record.workspace_id, str(record.session_id)
        ):
            raise MemoryRepositoryError("run ownership or workspace scope mismatch")
        try:
            self._request("POST", "eventlens_run_records", json_body=record.model_dump(mode="json"),
                          headers={"Prefer": "return=minimal"})
        except MemoryRepositoryError as exc:
            # A concurrent insert of the same revision is immutable too.
            current = self.get_run_record(record.workspace_id, record.run_id, record.kind, record.revision)
            if current == record:
                return record
            raise ImmutableRevisionError("durable run record revision already exists") from exc
        return record

    def get_run_record(self, workspace_id: str, run_id: UUID, kind: RunRecordKind, revision: int) -> DurableRunRecord | None:
        rows = self._request("GET", "eventlens_run_records", params={
            "select": "*", "workspace_id": f"eq.{workspace_id}", "run_id": f"eq.{run_id}",
            "kind": f"eq.{kind.value}", "revision": f"eq.{revision}", "limit": "1",
        })
        if not isinstance(rows, list):
            raise MemoryRepositoryError("Supabase returned invalid run records")
        return DurableRunRecord.model_validate(rows[0]) if rows else None

    def list_run_records(self, workspace_id: str, run_id: UUID, *, kind: RunRecordKind | None = None) -> tuple[DurableRunRecord, ...]:
        params = {"select": "*", "workspace_id": f"eq.{workspace_id}", "run_id": f"eq.{run_id}",
                  "order": "kind.asc,revision.asc"}
        if kind is not None:
            params["kind"] = f"eq.{kind.value}"
        rows = self._request("GET", "eventlens_run_records", params=params)
        if not isinstance(rows, list):
            raise MemoryRepositoryError("Supabase returned invalid run records")
        try:
            return tuple(DurableRunRecord.model_validate(row) for row in rows)
        except (TypeError, ValueError):
            raise MemoryRepositoryError("Supabase returned invalid run records") from None

    def save_source(self, record: SourceRecord) -> SourceRecord:
        return self._save("sources", record)

    def save_source_passage(self, record: SourcePassageRecord) -> SourcePassageRecord:
        return self._save("source_passages", record)

    def save_event(self, record: EarningsEventRecord, *, source_links: tuple[EventSourceLink, ...] = ()) -> EarningsEventRecord:
        result = self._save("earnings_events", record)
        for link in source_links:
            self._upsert_link("event_sources", {"event_id": str(record.id), **link.model_dump(mode="json")})
        return result

    def save_metric(self, record: EventMetricRecord) -> EventMetricRecord:
        return self._save("event_metrics", record)

    def save_market_setup(self, record: MarketSetupRecord, *, source_ids: tuple[UUID, ...] = ()) -> MarketSetupRecord:
        result = self._save("market_setups", record)
        for source_id in source_ids:
            self._upsert_link("market_setup_sources", {"market_setup_id": str(record.id), "source_id": str(source_id)})
        return result

    def save_market_reaction(self, record: MarketReactionRecord, *, source_ids: tuple[UUID, ...] = ()) -> MarketReactionRecord:
        result = self._save("market_reactions", record)
        for source_id in source_ids:
            self._upsert_link("market_reaction_sources", {"market_reaction_id": str(record.id), "source_id": str(source_id)})
        return result

    def save_session(self, record: ResearchSessionRecord) -> ResearchSessionRecord:
        return self._save("research_sessions", record)

    def save_hypothesis(self, record: HypothesisRecord) -> HypothesisRecord:
        self._require_draft_first("hypotheses", record)
        return self._save("hypotheses", record)

    def save_snapshot(self, record: PreEventSnapshotRecord, *, evidence_links: tuple[EvidenceLink, ...] = (), metric_ids: tuple[UUID, ...] = (), replace_links: bool = False) -> PreEventSnapshotRecord:
        self._require_draft_first("pre_event_snapshots", record)
        result = self._save("pre_event_snapshots", record)
        if record.status is VersionStatus.DRAFT:
            if replace_links:
                self._delete_links("pre_event_snapshot_evidence", "snapshot_id", record.id)
                self._delete_links("pre_event_snapshot_metrics", "snapshot_id", record.id)
            for link in evidence_links:
                self._upsert_link("pre_event_snapshot_evidence", {
                    "snapshot_id": str(record.id), "source_id": str(link.source_id), "passage_id": link.passage_id,
                    "relationship": link.relationship, "inclusion_reason": link.inclusion_reason or link.relationship,
                    "available_at": link.available_at.isoformat() if link.available_at else record.information_cutoff_at.isoformat(),
                })
            for metric_id in metric_ids:
                self._upsert_link("pre_event_snapshot_metrics", {"snapshot_id": str(record.id), "event_metric_id": str(metric_id)})
        return result

    def save_hypothesis_assessment(self, record: HypothesisAssessmentRecord, *, evidence_links: tuple[EvidenceLink, ...] = (), replace_links: bool = False) -> HypothesisAssessmentRecord:
        self._require_draft_first("hypothesis_assessments", record)
        result = self._save("hypothesis_assessments", record)
        if record.status is VersionStatus.DRAFT:
            if replace_links:
                self._delete_links(
                    "hypothesis_assessment_evidence",
                    "hypothesis_assessment_id",
                    record.id,
                )
            for link in evidence_links:
                if link.relationship not in {"supports", "contradicts", "context"}:
                    raise MemoryRepositoryError("assessment evidence has invalid stance")
                self._upsert_link("hypothesis_assessment_evidence", {"hypothesis_assessment_id": str(record.id), "source_id": str(link.source_id), "passage_id": link.passage_id, "stance": link.relationship})
        return result

    def save_context_card(self, record: EventContextCardRecord, *, evidence_links: tuple[EvidenceLink, ...] = (), replace_links: bool = False) -> EventContextCardRecord:
        self._require_draft_first("event_context_cards", record)
        result = self._save("event_context_cards", record)
        if record.status is ContextCardStatus.DRAFT:
            if replace_links:
                self._delete_links("context_card_evidence", "context_card_id", record.id)
            for link in evidence_links:
                if link.relationship not in {"supports", "contradicts", "context", "market_data"}:
                    raise MemoryRepositoryError("context-card evidence has invalid relationship")
                self._upsert_link("context_card_evidence", {"context_card_id": str(record.id), "source_id": str(link.source_id), "passage_id": link.passage_id, "relationship": link.relationship})
        return result

    def load_session_bundle(self, session_id: UUID) -> EventMemoryBundle:
        session = self._one("research_sessions", "id", session_id)
        if not isinstance(session, ResearchSessionRecord):
            raise MemoryRepositoryError("research session was not found")
        event = self._one("earnings_events", "id", session.event_id)
        company = self._one("companies", "id", session.company_id)
        if not isinstance(event, EarningsEventRecord) or not isinstance(company, CompanyRecord):
            raise MemoryRepositoryError("research session has incomplete lineage")
        hypotheses = tuple(self._many("hypotheses", "research_session_id", session.id))
        assessments = tuple(item for hypothesis in hypotheses for item in self._many("hypothesis_assessments", "hypothesis_id", hypothesis.id))
        snapshots = tuple(self._many("pre_event_snapshots", "research_session_id", session.id))
        cards = tuple(self._many("event_context_cards", "research_session_id", session.id))
        event_links = tuple(EventSourceLink.model_validate(row) for row in self._links("event_sources", "event_id", event.id, exclude={"event_id", "created_at"}))
        sources = tuple(source for link in event_links if isinstance((source := self._one("sources", "id", link.source_id)), SourceRecord))
        passages = tuple(
            passage
            for source in sources
            for passage in self._many("source_passages", "source_id", source.id)
        )
        setups = self._many("market_setups", "event_id", event.id)
        reactions = self._many("market_reactions", "event_id", event.id)
        return EventMemoryBundle(
            company=company, event=event, metrics=tuple(self._many("event_metrics", "event_id", event.id)),
            market_setup=setups[-1] if setups else None, market_reaction=reactions[-1] if reactions else None,
            sources=sources, source_passages=passages, session=session, hypotheses=hypotheses, hypothesis_assessments=assessments,
            snapshots=snapshots, context_cards=cards, event_source_links=event_links,
            snapshot_evidence={item.id: tuple(self._evidence("pre_event_snapshot_evidence", "snapshot_id", item.id)) for item in snapshots},
            assessment_evidence={item.id: tuple(self._evidence("hypothesis_assessment_evidence", "hypothesis_assessment_id", item.id)) for item in assessments},
            context_card_evidence={item.id: tuple(self._evidence("context_card_evidence", "context_card_id", item.id)) for item in cards},
        )

    def list_prior_context_cards(self, ticker: str, before_event_date, cutoff_at, *, limit: int = 12) -> tuple[EventContextCardRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        companies = self._request(
            "GET", "companies",
            params={"select": "id,ticker", "ticker": f"eq.{ticker.upper()}"},
        )
        if not isinstance(companies, list):
            raise MemoryRepositoryError("Supabase returned invalid company data")
        company_ids = [str(row["id"]) for row in companies if row.get("id")]
        if not company_ids:
            return ()
        events = self._request(
            "GET", "earnings_events",
            params={
                "select": "id,event_date,external_event_id,company_id",
                "company_id": f"in.({','.join(company_ids)})",
                "event_date": f"lt.{before_event_date.isoformat()}",
            },
        )
        if not isinstance(events, list):
            raise MemoryRepositoryError("Supabase returned invalid earnings-event data")
        event_by_id = {str(row["id"]): row for row in events if row.get("id")}
        if not event_by_id:
            return ()
        rows = self._request(
            "GET", "event_context_cards",
            params={
                "select": "*",
                "status": "eq.approved",
                "event_id": f"in.({','.join(event_by_id)})",
                "knowledge_cutoff_at": f"lte.{cutoff_at.isoformat()}",
                "order": "knowledge_cutoff_at.desc,id.desc",
                # Revisions are deduplicated after hydration; fetch a bounded
                # wider window so one event's revisions cannot consume the
                # requested distinct-event limit.
                "limit": str(max(limit * 4, 48)),
            },
        )
        if not isinstance(rows, list):
            raise MemoryRepositoryError("Supabase returned invalid context-card data")
        cards = []
        for row in rows:
            card = EventContextCardRecord.model_validate(
                {key: row[key] for key in EventContextCardRecord.model_fields if key in row}
            )
            event = event_by_id.get(str(card.event_id))
            if event is None:
                continue
            cards.append(card.model_copy(update={
                "ticker": ticker.upper(),
                "event_date": date.fromisoformat(event["event_date"]),
                "external_event_id": event.get("external_event_id"),
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

    def relation_sizes(self) -> tuple[RelationSizeRecord, ...]:
        rows = self._rpc("eventlens_relation_sizes")
        if not isinstance(rows, list):
            raise MemoryRepositoryError("Supabase returned invalid relation sizes")
        return tuple(RelationSizeRecord.model_validate(row) for row in rows)

    def evidence_storage_size(self) -> EvidenceStorageSizeRecord:
        rows = self._rpc("eventlens_evidence_storage_size")
        if not isinstance(rows, list) or not rows:
            raise MemoryRepositoryError("Supabase returned invalid evidence storage size")
        return EvidenceStorageSizeRecord.model_validate(rows[0])

    def _save(self, table: str, record: RecordT) -> RecordT:
        existing = self._one(table, "id", record.id)
        if existing is not None:
            if table in APPEND_ONLY:
                if existing == record:
                    return record
                raise ImmutableRevisionError(f"{table} revision is immutable")
            if _requires_frozen_content(record) and not _transition_preserves_content(existing, record):
                raise ImmutableRevisionError(f"{table} finalization changed reviewed content")
            if _terminal(existing) and existing != record and not _allowed_terminal_transition(existing, record):
                raise ImmutableRevisionError(f"{table} revision is immutable")
            if existing == record:
                return record
            self._request(
                "PATCH",
                table,
                params={"id": f"eq.{record.id}"},
                json_body=_to_postgrest_row(record),
                headers={"Prefer": "return=minimal"},
            )
            return record
        self._request(
            "POST",
            table,
            json_body=_to_postgrest_row(record),
            headers={"Prefer": "return=minimal"},
        )
        return record

    def _require_draft_first(self, table: str, record: BaseModel) -> None:
        if _requires_draft_first(record) and self._one(table, "id", record.id) is None:
            raise MemoryRepositoryError(f"{table} terminal revision must first be saved as draft")

    def _one(self, table: str, field: str, value: UUID) -> BaseModel | None:
        rows = self._many(table, field, value, limit=1)
        return rows[0] if rows else None

    def _many(self, table: str, field: str, value: UUID, *, limit: int | None = None) -> list[BaseModel]:
        if table not in TABLE_MODELS or field not in {"id", "event_id", "research_session_id", "hypothesis_id", "source_id"}:
            raise ValueError("unsupported memory query")
        params = {"select": "*", field: f"eq.{value}", "order": "created_at.asc,id.asc"}
        if limit is not None:
            params["limit"] = str(limit)
        rows = self._request("GET", table, params=params)
        if not isinstance(rows, list):
            raise MemoryRepositoryError(f"Supabase returned invalid data for {table}")
        try:
            # Optional v4 lifecycle columns may not exist until the forward
            # migration is applied.  Preserve read access to legacy rows while
            # Pydantic supplies their explicit backward-compatible defaults.
            return [
                TABLE_MODELS[table].model_validate(
                    {key: row[key] for key in TABLE_MODELS[table].model_fields if key in row}
                )
                for row in rows
            ]
        except (KeyError, TypeError, ValueError):
            raise MemoryRepositoryError(f"Supabase returned invalid persisted data for {table}") from None

    def _upsert_link(self, table: str, row: dict[str, Any]) -> None:
        self._request("POST", table, json_body=row, headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})

    def _delete_links(self, table: str, field: str, value: UUID) -> None:
        self._request(
            "DELETE",
            table,
            params={field: f"eq.{value}"},
            headers={"Prefer": "return=minimal"},
        )

    def _links(self, table: str, field: str, value: UUID, *, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        rows = self._request("GET", table, params={"select": "*", field: f"eq.{value}"})
        if not isinstance(rows, list):
            raise MemoryRepositoryError(f"Supabase returned invalid links for {table}")
        return [{key: val for key, val in row.items() if key not in (exclude or set())} for row in rows]

    def _evidence(self, table: str, field: str, value: UUID) -> list[EvidenceLink]:
        output: list[EvidenceLink] = []
        for row in self._links(table, field, value):
            source = self._one("sources", "id", UUID(row["source_id"]))
            if not isinstance(source, SourceRecord):
                raise MemoryRepositoryError("evidence references missing source")
            output.append(EvidenceLink(source_id=source.id, passage_id=row["passage_id"], relationship=row.get("relationship", row.get("stance")), inclusion_reason=row.get("inclusion_reason"), available_at=row.get("available_at", source.available_at)))
        return output

    def _request(self, method: str, table: str, *, params: dict[str, str] | None = None, json_body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        try:
            response = self._client.request(method, f"{self._base_url}/{quote(table, safe='/')}", params=params, json=json_body, headers={**self._headers, **(headers or {})})
        except httpx.HTTPError:
            raise MemoryRepositoryError(f"Supabase request failed before a response for {table}") from None
        if response.status_code >= 400:
            raise MemoryRepositoryError(f"Supabase request failed with HTTP {response.status_code} for {table}")
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            raise MemoryRepositoryError(f"Supabase returned non-JSON data for {table}") from None

    def _rpc(self, function_name: str) -> Any:
        return self._request("POST", f"rpc/{function_name}", json_body={})


def _to_postgrest_row(record: BaseModel) -> dict[str, Any]:
    """Every key maps one-for-one to a column in the checked-in migration."""
    projection_only = {"ticker", "event_date", "external_event_id"}
    excluded = projection_only if isinstance(record, EventContextCardRecord) else set()
    return record.model_dump(mode="json", exclude_none=False, exclude=excluded)


class SupabaseEvidenceStore:
    """Checksum-addressed byte storage for source bodies in the private bucket."""

    bucket_name = "eventlens-evidence"

    def __init__(
        self,
        *,
        url: str,
        secret_key: SecretStr | str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        normalized = url.strip().rstrip("/")
        secret = (secret_key.get_secret_value() if isinstance(secret_key, SecretStr) else secret_key).strip()
        if not normalized or not secret:
            raise ValueError("Supabase URL and server secret key are required")
        self._base_url = f"{normalized}/storage/v1/object/{self.bucket_name}"
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._headers = {"apikey": secret, "Authorization": f"Bearer {secret}"}

    def __repr__(self) -> str:
        return "SupabaseEvidenceStore(configured=True)"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def put_bytes(self, content: bytes, *, media_type: str) -> tuple[str, str, int]:
        digest = hashlib.sha256(content).hexdigest()
        path = f"sha256/{digest}"
        try:
            response = self._client.request(
                "POST",
                f"{self._base_url}/{quote(path, safe='/')}",
                content=content,
                headers={**self._headers, "Content-Type": media_type, "x-upsert": "false"},
            )
        except httpx.HTTPError:
            raise MemoryRepositoryError("Supabase evidence upload failed before a response") from None
        if response.status_code in {400, 409} and self.get_bytes(digest) == content:
            return (f"supabase-storage://{self.bucket_name}/{path}", digest, len(content))
        if response.status_code >= 400:
            raise MemoryRepositoryError(
                f"Supabase evidence upload failed with HTTP {response.status_code}"
            )
        return (f"supabase-storage://{self.bucket_name}/{path}", digest, len(content))

    def get_bytes(self, content_sha256: str) -> bytes | None:
        if len(content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in content_sha256
        ):
            raise ValueError("content_sha256 must be a SHA-256 hex digest")
        try:
            response = self._client.request(
                "GET",
                f"{self._base_url}/sha256/{quote(content_sha256, safe='')}",
                headers=self._headers,
            )
        except httpx.HTTPError:
            raise MemoryRepositoryError("Supabase evidence download failed before a response") from None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise MemoryRepositoryError(
                f"Supabase evidence download failed with HTTP {response.status_code}"
            )
        content = response.content
        if hashlib.sha256(content).hexdigest() != content_sha256:
            raise MemoryRepositoryError("Supabase evidence checksum mismatch")
        return content
