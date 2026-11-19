from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from eventlens.persistence.memory_models import (
    ContextCardStatus,
    DataKind,
    EventContextCardRecord,
    RecommendationAction,
    ResearchPhase,
    ResearchSessionRecord,
    RunMode,
)


MIGRATION = (
    Path(__file__).parents[2]
    / "supabase"
    / "migrations"
    / "20260829000100_durable_research_memory.sql"
)
PASSAGE_MIGRATION = (
    Path(__file__).parents[2]
    / "supabase"
    / "migrations"
    / "20260829000300_source_passages.sql"
)
PASSAGE_INTEGRITY_MIGRATION = (
    Path(__file__).parents[2]
    / "supabase"
    / "migrations"
    / "20260829000400_enforce_passage_integrity.sql"
)
V4_LIFECYCLE_MIGRATION = (
    Path(__file__).parents[2]
    / "supabase"
    / "migrations"
    / "20260913000100_context_card_v4_lifecycle.sql"
)
HISTORICAL_CONTEXT_LINEAGE_MIGRATION = (
    Path(__file__).parents[2]
    / "supabase"
    / "migrations"
    / "20261012000100_historical_context_lineage.sql"
)
RUN_GRAPH_MIGRATION = (
    Path(__file__).parents[2]
    / "supabase"
    / "migrations"
    / "20261030000100_durable_run_graph_records.sql"
)
SHA = "a" * 64
REQUIRED_TABLES = {
    "companies",
    "earnings_events",
    "event_metrics",
    "market_setups",
    "market_reactions",
    "sources",
    "research_sessions",
    "hypotheses",
    "pre_event_snapshots",
    "hypothesis_assessments",
    "event_context_cards",
    "event_sources",
    "market_setup_sources",
    "market_reaction_sources",
    "pre_event_snapshot_evidence",
    "pre_event_snapshot_metrics",
    "hypothesis_assessment_evidence",
    "context_card_evidence",
}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _passage_sql() -> str:
    return PASSAGE_MIGRATION.read_text(encoding="utf-8")


def _passage_integrity_sql() -> str:
    return PASSAGE_INTEGRITY_MIGRATION.read_text(encoding="utf-8")


def _v4_lifecycle_sql() -> str:
    return V4_LIFECYCLE_MIGRATION.read_text(encoding="utf-8")


def test_historical_context_lineage_migration_covers_all_decision_artifacts() -> None:
    sql = HISTORICAL_CONTEXT_LINEAGE_MIGRATION.read_text(encoding="utf-8")
    for table in ("hypotheses", "pre_event_snapshots", "event_context_cards"):
        assert f"alter table public.{table}" in sql
        assert f"{table}_historical_context_manifest_valid" in sql
        assert f"{table}_historical_context_manifest_sha_idx" in sql
    assert sql.count("historical_context_manifest jsonb") == 3
    assert sql.count("historical_context_manifest_sha256 text") == 3


def test_run_graph_migration_is_scoped_immutable_and_private() -> None:
    sql = RUN_GRAPH_MIGRATION.read_text(encoding="utf-8").lower()
    assert "eventlens.run_record.v1" in sql
    assert "owner_id text not null" in sql and "workspace_id text not null" in sql
    assert "unique (workspace_id, run_id, kind, revision)" in sql
    assert "before update or delete" in sql
    assert "alter table public.eventlens_run_records enable row level security" in sql
    assert "revoke all on table public.eventlens_run_records from public, anon, authenticated" in sql
    assert "grant select, insert on table public.eventlens_run_records to service_role" in sql
    assert "create policy" not in sql
    assert "octet_length(metadata::text) <= 32768" in sql


def test_v4_lifecycle_migration_is_forward_only_and_auditable() -> None:
    sql = _v4_lifecycle_sql()

    assert "drop constraint if exists event_context_cards_status_check" in sql
    assert "status in ('draft', 'blocked', 'reviewed', 'approved', 'stale')" in sql
    assert "create table if not exists public.context_card_lifecycle_audit" in sql
    assert "context_card_lifecycle_audit_append_only" in sql
    assert "event_context_cards_audit_v4_lifecycle" in sql
    assert "approved context card % is immutable except v4 staleness" in sql
    assert "schema_version !~ '^eventlens[.]context_card[.]v4'" in sql
    assert "and status in ('draft', 'approved')" in sql
    assert "create or replace function public.eventlens_protect_approved_card()" in sql
    assert "evidence links for terminal context card % are immutable" in sql
    assert "context_card_lifecycle_audit_append_only" in sql


def test_migration_contains_required_tables_uuid_keys_and_rls_without_policies() -> None:
    sql = _sql()

    created = set(re.findall(r"create table public\.([a-z_]+)", sql))
    assert REQUIRED_TABLES <= created
    for table in REQUIRED_TABLES:
        assert f"alter table public.{table} enable row level security;" in sql
    assert "create policy" not in sql.lower()
    assert "id uuid primary key default gen_random_uuid()" in sql
    assert sql.count("timestamptz") >= 25


def test_migration_enforces_cutoff_identity_and_transitive_immutability() -> None:
    sql = _sql()

    assert "locking hypothesis % cannot rewrite its reviewed content or identity" in sql
    assert "finalizing snapshot % cannot rewrite its reviewed content or identity" in sql
    assert "finalizing assessment % cannot rewrite its reviewed content or identity" in sql
    assert "approving context card % cannot rewrite its reviewed content or identity" in sql
    assert "snapshot % contains evidence unavailable at its cutoff" in sql
    assert "snapshot % contains unavailable or cross-event metrics" in sql
    assert "context card % has inconsistent or non-final object references" in sql
    assert "sources_append_only" in sql
    assert "event_metrics_append_only" in sql
    assert "market_setups_append_only" in sql
    assert "market_reactions_append_only" in sql


def test_transition_functions_reference_only_columns_from_their_own_rows() -> None:
    sql = _sql()

    snapshot_function = sql.split(
        "create or replace function public.eventlens_protect_finalized_snapshot()", 1
    )[1].split("create trigger pre_event_snapshots_protect_finalized", 1)[0]
    for assessment_only_column in (
        "new.hypothesis_id",
        "new.snapshot_id",
        "new.conclusion",
        "new.model_provider",
        "new.knowledge_cutoff_at",
    ):
        assert assessment_only_column not in snapshot_function

    assessment_function = sql.split(
        "create or replace function public.eventlens_protect_finalized_assessment()", 1
    )[1].split("create trigger hypothesis_assessments_protect_finalized", 1)[0]
    assert "new.hypothesis_id" in assessment_function
    assert "new.snapshot_id" in assessment_function
    assert "new.knowledge_cutoff_at" in assessment_function


def test_migration_defines_private_content_addressed_storage_and_size_rpc() -> None:
    sql = _sql()

    assert "values ('eventlens-evidence', 'eventlens-evidence', false)" in sql
    assert "supabase-storage://eventlens-evidence/sha256/" in sql
    assert "create or replace function public.eventlens_relation_sizes()" in sql
    assert "revoke all on function public.eventlens_relation_sizes() from public, anon, authenticated" in sql
    assert "grant execute on function public.eventlens_relation_sizes() to service_role" in sql
    assert "create or replace function public.eventlens_evidence_storage_size()" in sql
    assert "grant execute on function public.eventlens_evidence_storage_size() to service_role" in sql


def test_passage_migration_defines_append_only_source_passages() -> None:
    sql = _passage_sql()

    assert "create table public.source_passages" in sql
    assert "source_id uuid not null references public.sources" in sql
    assert "constraint source_passages_source_passage_unique unique (source_id, passage_id)" in sql
    assert "create trigger source_passages_append_only" in sql
    assert "alter table public.source_passages enable row level security;" in sql
    assert "grant select, insert, update, delete on table public.source_passages to service_role" in sql
    assert "'source_passages'" in sql


def test_passage_integrity_migration_enforces_new_evidence_links() -> None:
    sql = _passage_integrity_sql()

    assert "source_passages_text_sha256_matches_text" in sql
    assert "extensions.digest(convert_to(\"text\", 'UTF8'), 'sha256')" in sql
    assert "pre_event_snapshot_evidence_passage_fk" in sql
    assert "hypothesis_assessment_evidence_passage_fk" in sql
    assert "context_card_evidence_passage_fk" in sql
    assert "references public.source_passages (source_id, passage_id)" in sql
    assert "not valid" in sql
    assert "from storage.objects" in sql


def _session(**changes: object) -> ResearchSessionRecord:
    actual = datetime(2026, 8, 29, 14, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "id": uuid4(),
        "session_key": "demo-fy2026-q2-replay",
        "company_id": uuid4(),
        "event_id": uuid4(),
        "run_mode": RunMode.REPLAY,
        "data_kind": DataKind.REAL,
        "phase": ResearchPhase.CREATED,
        "information_cutoff_at": datetime(2026, 1, 21, 21, tzinfo=timezone.utc),
        "replay_as_of_at": datetime(2026, 1, 21, 21, tzinfo=timezone.utc),
        "actual_started_at": actual,
        "last_transition_at": actual,
        "compact_state": {},
        "state_sha256": SHA,
        "workflow_version": "1.0",
        "schema_version": "1.0",
        "created_at": actual,
        "updated_at": actual,
    }
    values.update(changes)
    return ResearchSessionRecord.model_validate(values)


def test_replay_and_live_clocks_are_not_interchangeable() -> None:
    replay = _session()
    assert replay.actual_started_at.year == 2026
    assert replay.replay_as_of_at is not None
    assert replay.replay_as_of_at.month == 1

    with pytest.raises(ValidationError, match="replay sessions require replay_as_of_at"):
        _session(replay_as_of_at=None)
    with pytest.raises(ValidationError, match="live sessions cannot carry replay_as_of_at"):
        _session(run_mode=RunMode.LIVE)


def test_context_card_taxonomy_distinguishes_trim_from_sell_and_validates_prices() -> None:
    actual = datetime(2026, 8, 29, 14, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "id": uuid4(),
        "event_id": uuid4(),
        "research_session_id": uuid4(),
        "snapshot_id": uuid4(),
        "hypothesis_id": uuid4(),
        "hypothesis_assessment_id": uuid4(),
        "market_setup_id": uuid4(),
        "market_reaction_id": uuid4(),
        "version": 1,
        "status": ContextCardStatus.DRAFT,
        "data_kind": DataKind.REAL,
        "run_mode": RunMode.REPLAY,
        "knowledge_cutoff_at": actual - timedelta(days=1),
        "replay_as_of_at": actual,
        "recommendation_action": RecommendationAction.TRIM,
        "recommendation_price_floor": Decimal("450"),
        "recommendation_price_ceiling": Decimal("500"),
        "payload": {"summary": "draft"},
        "content_sha256": SHA,
        "prompt_version": "context-card-v1",
        "model_provider": "deterministic-test",
        "model_name": "fixture",
        "schema_version": "1.0",
        "actual_created_at": actual,
        "approved_at": None,
        "approved_by": None,
        "created_at": actual,
        "updated_at": actual,
    }
    card = EventContextCardRecord.model_validate(values)
    assert card.recommendation_action is RecommendationAction.TRIM
    assert RecommendationAction.TRIM is not RecommendationAction.SELL

    values["recommendation_price_floor"] = Decimal("501")
    with pytest.raises(ValidationError, match="price floor cannot exceed ceiling"):
        EventContextCardRecord.model_validate(values)


def test_v4_neutral_approval_and_decision_trace_gate_preserve_legacy_taxonomy() -> None:
    actual = datetime(2026, 8, 29, 14, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "id": uuid4(), "event_id": uuid4(), "research_session_id": uuid4(),
        "snapshot_id": uuid4(), "hypothesis_id": uuid4(), "hypothesis_assessment_id": uuid4(),
        "market_setup_id": uuid4(), "market_reaction_id": uuid4(), "version": 1,
        "status": ContextCardStatus.APPROVED, "data_kind": DataKind.REAL,
        "run_mode": RunMode.REPLAY, "knowledge_cutoff_at": actual - timedelta(days=1),
        "replay_as_of_at": actual, "recommendation_action": None,
        "recommendation_price_floor": None, "recommendation_price_ceiling": None,
        "decision_trace": None, "payload": {"summary": "neutral"}, "content_sha256": SHA,
        "prompt_version": "context-card-v4", "model_provider": "deterministic-test",
        "model_name": "fixture", "schema_version": "eventlens.context_card.v4",
        "actual_created_at": actual, "reviewed_at": None, "reviewed_by": None,
        "approved_at": actual, "approved_by": "analyst", "stale_at": None,
        "created_at": actual, "updated_at": actual,
    }
    assert EventContextCardRecord.model_validate(values).status is ContextCardStatus.APPROVED

    values["recommendation_action"] = RecommendationAction.TRIM
    with pytest.raises(ValidationError, match="decision-bearing v4 context card requires decision_trace"):
        EventContextCardRecord.model_validate(values)
    values["decision_trace"] = {"inputs": ["claim-1"]}
    assert EventContextCardRecord.model_validate(values).decision_trace == {"inputs": ["claim-1"]}

    values["schema_version"] = "1.0"
    values["status"] = ContextCardStatus.REVIEWED
    with pytest.raises(ValidationError, match="legacy context cards support only draft or approved status"):
        EventContextCardRecord.model_validate(values)
