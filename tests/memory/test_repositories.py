from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

import eventlens.memory as memory
from eventlens.config import EventLensSettings
from eventlens.domain.events import EventTiming
from eventlens.memory.supabase import _to_postgrest_row


UTC = timezone.utc
NOW = datetime(2026, 8, 29, 14, tzinfo=UTC)
CUTOFF = datetime(2026, 1, 21, 21, tzinfo=UTC)
SHA = "a" * 64


def _records() -> dict[str, object]:
    company = memory.CompanyRecord(
        id=uuid4(), ticker="DEMO", exchange="XNAS", market="US",
        legal_name="Example Systems Corporation", primary_currency="USD", schema_version="1.0",
        created_at=NOW, updated_at=NOW,
    )
    source = memory.SourceRecord(
        id=uuid4(), provider="sec", external_source_id="demo-8k", source_type="sec_filing",
        title="DEMO 8-K", canonical_url="https://example.test/8k", published_at=CUTOFF - timedelta(days=1),
        available_at=CUTOFF - timedelta(days=1), retrieved_at=NOW, content_sha256=SHA,
        artifact_reference="local://sha256/" + SHA, size_bytes=123, media_type="text/html",
        data_kind="real", schema_version="1.0", created_at=NOW,
    )
    event = memory.EarningsEventRecord(
        id=uuid4(), company_id=company.id, external_event_id="DEMO:2026-01-28:2026Q2",
        fiscal_year=2026, fiscal_quarter=2, fiscal_period_end=date(2025, 12, 31),
        event_date=date(2026, 1, 28), timing=EventTiming.AFTER_MARKET_CLOSE,
        release_at=datetime(2026, 1, 28, 21, 4, tzinfo=UTC), available_at=datetime(2026, 1, 28, 21, 4, tzinfo=UTC),
        timing_confirmed=True, data_kind="real", schema_version="1.0", created_at=NOW, updated_at=NOW,
    )
    metric = memory.EventMetricRecord(
        id=uuid4(), event_id=event.id, source_id=source.id, metric_key="consensus_revenue_estimate",
        metric_version=1, value_numeric="69632000000", available_at=CUTOFF - timedelta(days=1),
        calculation_version="pre-event-consensus-v1", provenance_sha256=SHA, data_kind="real",
        schema_version="1.0", created_at=NOW,
    )
    setup = memory.MarketSetupRecord(
        id=uuid4(), event_id=event.id, setup_version=1, as_of_at=CUTOFF,
        information_cutoff_at=CUTOFF, available_at=CUTOFF, payload={"price": "446.20"},
        payload_sha256=SHA, calculation_version="setup-v1", data_kind="real",
        schema_version="1.0", created_at=NOW,
    )
    reaction = memory.MarketReactionRecord(
        id=uuid4(), event_id=event.id, reaction_version=1, reference_session=date(2026, 1, 28),
        first_post_event_session=date(2026, 1, 29), fifth_post_event_session=date(2026, 2, 4),
        available_at=datetime(2026, 2, 5, tzinfo=UTC), payload={"five_day_return": "-0.04"},
        payload_sha256=SHA, calculation_version="reaction-v1", data_kind="real",
        schema_version="1.0", created_at=NOW,
    )
    session = memory.ResearchSessionRecord(
        id=uuid4(), session_key="demo-fy2026-q2-replay", company_id=company.id, event_id=event.id,
        run_mode="replay", data_kind="real", phase="created", information_cutoff_at=CUTOFF,
        replay_as_of_at=CUTOFF, actual_started_at=NOW, last_transition_at=NOW,
        compact_state={"phase": "created"}, state_sha256=SHA, workflow_version="1.0",
        schema_version="1.0", created_at=NOW, updated_at=NOW,
    )
    hypothesis = memory.HypothesisRecord(
        id=uuid4(), research_session_id=session.id, version=1, status="draft", author_mode="analyst",
        run_mode="replay", content={"base_case": "cloud capacity constrains growth"}, content_sha256=SHA,
        evidence_cutoff_at=CUTOFF, replay_as_of_at=CUTOFF, actual_created_at=NOW,
        schema_version="1.0", created_at=NOW, updated_at=NOW,
    )
    snapshot = memory.PreEventSnapshotRecord(
        id=uuid4(), research_session_id=session.id, event_id=event.id, version=1, status="draft",
        run_mode="replay", data_kind="real", information_cutoff_at=CUTOFF, replay_as_of_at=CUTOFF,
        payload={"claims": ["capacity constrained"]}, content_sha256=SHA, calculation_version="snapshot-v1",
        workflow_version="1.0", schema_version="1.0", actual_created_at=NOW, created_at=NOW, updated_at=NOW,
    )
    assessment = memory.HypothesisAssessmentRecord(
        id=uuid4(), hypothesis_id=hypothesis.id, research_session_id=session.id, snapshot_id=snapshot.id,
        version=1, status="draft", conclusion="weakened", run_mode="replay", payload={"reason": "mixed evidence"},
        content_sha256=SHA, prompt_version="assessment-v1", model_provider="fixture", model_name="deterministic",
        schema_version="1.0", knowledge_cutoff_at=CUTOFF, replay_as_of_at=CUTOFF,
        actual_created_at=NOW, created_at=NOW, updated_at=NOW,
    )
    return locals()


def test_local_repository_rehydrates_fresh_process_and_preserves_lifecycle(tmp_path: Path) -> None:
    r = _records()
    path = tmp_path / "memory.sqlite"
    repo = memory.SQLiteResearchMemoryRepository(path)
    repo.save_company(r["company"])
    repo.save_source(r["source"])
    repo.save_event(r["event"], source_links=(memory.EventSourceLink(source_id=r["source"].id, relationship="primary_filing", is_primary=True),))
    repo.save_metric(r["metric"])
    repo.save_market_setup(r["setup"], source_ids=(r["source"].id,))
    repo.save_market_reaction(r["reaction"], source_ids=(r["source"].id,))
    repo.save_session(r["session"])
    repo.save_hypothesis(r["hypothesis"])
    locked = r["hypothesis"].model_copy(update={"status": memory.HypothesisStatus.LOCKED, "approved_at": NOW, "approved_by": "analyst"})
    repo.save_hypothesis(locked)
    link = memory.EvidenceLink(source_id=r["source"].id, passage_id="p1", relationship="supports", available_at=r["source"].available_at, inclusion_reason="direct statement")
    repo.save_snapshot(r["snapshot"], evidence_links=(link,), metric_ids=(r["metric"].id,))
    finalized_snapshot = r["snapshot"].model_copy(update={"status": memory.VersionStatus.FINALIZED, "finalized_at": NOW, "approved_at": NOW, "approved_by": "analyst"})
    repo.save_snapshot(finalized_snapshot)
    repo.save_hypothesis_assessment(r["assessment"], evidence_links=(link,))
    finalized_assessment = r["assessment"].model_copy(update={"status": memory.VersionStatus.FINALIZED, "finalized_at": NOW})
    repo.save_hypothesis_assessment(finalized_assessment)
    card = memory.EventContextCardRecord(
        id=uuid4(), event_id=r["event"].id, research_session_id=r["session"].id,
        snapshot_id=r["snapshot"].id, hypothesis_id=r["hypothesis"].id,
        hypothesis_assessment_id=r["assessment"].id, market_setup_id=r["setup"].id,
        market_reaction_id=r["reaction"].id, version=1, status="draft", data_kind="real",
        run_mode="replay", knowledge_cutoff_at=r["reaction"].available_at, replay_as_of_at=NOW,
        recommendation_action="DONT_BUY", payload={"summary": "wait"}, content_sha256=SHA,
        prompt_version="card-v1", model_provider="fixture", model_name="deterministic",
        schema_version="1.0", actual_created_at=NOW, created_at=NOW, updated_at=NOW,
    )
    repo.save_context_card(card, evidence_links=(link,))
    repo.save_context_card(card.model_copy(update={"status": memory.ContextCardStatus.APPROVED, "approved_at": NOW, "approved_by": "analyst"}))

    fresh = memory.SQLiteResearchMemoryRepository(path)
    bundle = fresh.load_session_bundle(r["session"].id)
    assert bundle.company.ticker == "DEMO"
    assert bundle.event.data_kind is memory.DataKind.REAL
    assert bundle.session.run_mode is memory.RunMode.REPLAY
    assert bundle.hypotheses[-1].status is memory.HypothesisStatus.LOCKED
    assert bundle.snapshots[-1].status is memory.VersionStatus.FINALIZED
    assert bundle.context_cards[-1].status is memory.ContextCardStatus.APPROVED
    assert bundle.snapshot_evidence[r["snapshot"].id][0].passage_id == "p1"
    prior = fresh.list_prior_context_cards("DEMO", date(2026, 2, 1), NOW, limit=12)
    assert prior and prior[0].id == card.id
    assert prior[0].ticker == "DEMO"
    assert prior[0].event_date == r["event"].event_date
    assert prior[0].external_event_id == r["event"].external_event_id
    revision = card.model_copy(update={
        "id": uuid4(),
        "version": 2,
        "knowledge_cutoff_at": CUTOFF - timedelta(days=1),
        "status": memory.ContextCardStatus.APPROVED,
        "approved_at": NOW,
        "approved_by": "analyst",
    })
    fresh.save_context_card(revision.model_copy(update={
        "status": memory.ContextCardStatus.DRAFT,
        "approved_at": None,
        "approved_by": None,
    }), evidence_links=(link,))
    fresh.save_context_card(revision)
    latest = fresh.list_prior_context_cards("DEMO", date(2026, 2, 1), NOW, limit=12)
    assert len(latest) == 1
    assert latest[0].id == revision.id
    assert latest[0].version == 2


def test_historical_context_lineage_survives_sqlite_restart_and_stays_consistent(
    tmp_path: Path,
) -> None:
    r = _records()
    lineage = (
        memory.HistoricalContextLineageEntry(
            card_id=str(uuid4()),
            event_id="DEMO:2025-10-29:2026Q1",
            ticker="DEMO",
            event_date=date(2025, 10, 29),
            knowledge_cutoff_at=CUTOFF - timedelta(days=80),
            content_sha256="b" * 64,
        ),
    )
    lineage_sha = memory.historical_context_manifest_sha256(lineage)
    hypothesis = r["hypothesis"].model_copy(
        update={
            "historical_context_manifest": lineage,
            "historical_context_manifest_sha256": lineage_sha,
        }
    )
    snapshot = r["snapshot"].model_copy(
        update={
            "historical_context_manifest": lineage,
            "historical_context_manifest_sha256": lineage_sha,
        }
    )
    assessment = r["assessment"].model_copy(
        update={"hypothesis_id": hypothesis.id, "snapshot_id": snapshot.id}
    )
    card = memory.EventContextCardRecord(
        id=uuid4(), event_id=r["event"].id, research_session_id=r["session"].id,
        snapshot_id=snapshot.id, hypothesis_id=hypothesis.id,
        hypothesis_assessment_id=assessment.id, market_setup_id=r["setup"].id,
        market_reaction_id=r["reaction"].id, version=1, status="draft", data_kind="real",
        run_mode="replay", knowledge_cutoff_at=CUTOFF, replay_as_of_at=CUTOFF,
        payload={"summary": "lineage test"}, content_sha256=SHA,
        historical_context_manifest=lineage,
        historical_context_manifest_sha256=lineage_sha,
        prompt_version="card-v1", model_provider="fixture", model_name="deterministic",
        schema_version="1.0", actual_created_at=NOW, created_at=NOW, updated_at=NOW,
    )
    path = tmp_path / "lineage.sqlite"
    repo = memory.SQLiteResearchMemoryRepository(path)
    repo.save_company(r["company"])
    repo.save_event(r["event"])
    repo.save_market_setup(r["setup"])
    repo.save_market_reaction(r["reaction"])
    repo.save_session(r["session"])
    repo.save_hypothesis(hypothesis)
    repo.save_snapshot(snapshot)
    repo.save_hypothesis_assessment(assessment)
    repo.save_context_card(card)

    bundle = memory.SQLiteResearchMemoryRepository(path).load_session_bundle(r["session"].id)
    assert bundle.hypotheses[0].historical_context_manifest_sha256 == lineage_sha
    assert bundle.snapshots[0].historical_context_manifest == lineage
    assert bundle.context_cards[0].historical_context_manifest == lineage


def test_historical_context_lineage_rejects_bad_hash_and_cross_artifact_drift(
    tmp_path: Path,
) -> None:
    r = _records()
    lineage = (
        memory.HistoricalContextLineageEntry(
            card_id=str(uuid4()), event_id="DEMO:prior", ticker="DEMO",
            event_date=date(2025, 10, 29), knowledge_cutoff_at=CUTOFF - timedelta(days=1),
            content_sha256="b" * 64,
        ),
    )
    with pytest.raises(ValueError, match="checksum does not match"):
        memory.HypothesisRecord.model_validate(
            {
                **r["hypothesis"].model_dump(),
                "historical_context_manifest": lineage,
                "historical_context_manifest_sha256": "c" * 64,
            }
        )

    lineage_sha = memory.historical_context_manifest_sha256(lineage)
    repo = memory.SQLiteResearchMemoryRepository(tmp_path / "drift.sqlite")
    repo.save_company(r["company"])
    repo.save_event(r["event"])
    repo.save_session(r["session"])
    hypothesis = r["hypothesis"].model_copy(
        update={
            "historical_context_manifest": lineage,
            "historical_context_manifest_sha256": lineage_sha,
        }
    )
    repo.save_hypothesis(hypothesis)
    repo.save_snapshot(r["snapshot"])
    with pytest.raises(memory.MemoryRepositoryError, match="historical context lineage differs"):
        repo.save_hypothesis_assessment(r["assessment"])


def _save_approved_prior_context_card(
    repo: memory.ResearchMemoryRepository,
    *,
    company: memory.CompanyRecord,
    event_date: date,
    external_event_id: str,
) -> memory.EventContextCardRecord:
    """Persist one fully valid approved prior card through the public adapter."""

    base = _records()
    event = base["event"].model_copy(update={
        "id": uuid4(), "company_id": company.id,
        "external_event_id": external_event_id, "event_date": event_date,
        "release_at": datetime.combine(event_date, datetime.min.time(), tzinfo=UTC) + timedelta(hours=21),
        "available_at": datetime.combine(event_date, datetime.min.time(), tzinfo=UTC) + timedelta(hours=21),
    })
    source = base["source"].model_copy(update={
        "id": uuid4(), "external_source_id": f"{external_event_id}-source",
    })
    metric = base["metric"].model_copy(update={"id": uuid4(), "event_id": event.id, "source_id": source.id})
    setup = base["setup"].model_copy(update={"id": uuid4(), "event_id": event.id})
    reaction = base["reaction"].model_copy(update={"id": uuid4(), "event_id": event.id})
    session = base["session"].model_copy(update={
        "id": uuid4(), "session_key": f"session-{external_event_id}",
        "company_id": company.id, "event_id": event.id,
    })
    hypothesis = base["hypothesis"].model_copy(update={"id": uuid4(), "research_session_id": session.id})
    snapshot = base["snapshot"].model_copy(update={
        "id": uuid4(), "research_session_id": session.id, "event_id": event.id,
    })
    assessment = base["assessment"].model_copy(update={
        "id": uuid4(), "hypothesis_id": hypothesis.id,
        "research_session_id": session.id, "snapshot_id": snapshot.id,
    })
    payload = {
        "human_readable": {
            "executive_summary": f"Historical summary for {external_event_id}",
            "key_messages": ["Retained prior-event evidence"],
            "interpretation": "Descriptive historical context only.",
        }
    }
    card = memory.EventContextCardRecord(
        id=uuid4(), event_id=event.id, research_session_id=session.id,
        snapshot_id=snapshot.id, hypothesis_id=hypothesis.id,
        hypothesis_assessment_id=assessment.id, market_setup_id=setup.id,
        market_reaction_id=reaction.id, version=1, status="draft", data_kind="real",
        run_mode="replay", knowledge_cutoff_at=CUTOFF, replay_as_of_at=NOW,
        recommendation_action="DONT_BUY", payload=payload,
        content_sha256=memory.canonical_sha256(payload), prompt_version="restart-test",
        model_provider="fixture", model_name="deterministic", schema_version="1.0",
        actual_created_at=NOW, created_at=NOW, updated_at=NOW,
    )
    link = memory.EvidenceLink(
        source_id=source.id, passage_id="prior-evidence", relationship="supports",
        available_at=source.available_at, inclusion_reason="persisted restart test",
    )
    repo.save_source(source)
    repo.save_event(event)
    repo.save_metric(metric)
    repo.save_market_setup(setup)
    repo.save_market_reaction(reaction)
    repo.save_session(session)
    repo.save_hypothesis(hypothesis)
    repo.save_hypothesis(hypothesis.model_copy(update={
        "status": memory.HypothesisStatus.LOCKED, "approved_at": NOW, "approved_by": "test",
    }))
    repo.save_snapshot(snapshot, evidence_links=(link,), metric_ids=(metric.id,))
    repo.save_snapshot(snapshot.model_copy(update={
        "status": memory.VersionStatus.FINALIZED, "finalized_at": NOW,
        "approved_at": NOW, "approved_by": "test",
    }))
    repo.save_hypothesis_assessment(assessment, evidence_links=(link,))
    repo.save_hypothesis_assessment(assessment.model_copy(update={
        "status": memory.VersionStatus.FINALIZED, "finalized_at": NOW,
    }))
    repo.save_context_card(card, evidence_links=(link,))
    return repo.save_context_card(card.model_copy(update={
        "status": memory.ContextCardStatus.APPROVED, "approved_at": NOW, "approved_by": "test",
    }))


def test_sqlite_close_reopen_lists_identical_cutoff_eligible_context_cards(
    tmp_path: Path,
) -> None:
    """Repository restart preserves card identity and cutoff filtering."""

    path = tmp_path / "restart.sqlite"
    original = memory.SQLiteResearchMemoryRepository(path)
    company = _records()["company"]
    original.save_company(company)
    cards = tuple(
        _save_approved_prior_context_card(
            original, company=company, event_date=event_date, external_event_id=event_id,
        )
        for event_date, event_id in (
            (date(2026, 6, 24), "DEMO:2026-06-24:2026Q4"),
            (date(2026, 3, 18), "DEMO:2026-03-18:2026Q3"),
            (date(2025, 12, 17), "DEMO:2025-12-17:2026Q2"),
        )
    )
    del original  # Each repository operation owns and closes its SQLite connection.

    target_date = date(2026, 9, 30)
    target_cutoff = datetime(2026, 9, 29, tzinfo=UTC)
    first = memory.SQLiteResearchMemoryRepository(path).list_prior_context_cards(
        "DEMO", target_date, target_cutoff,
    )
    second = memory.SQLiteResearchMemoryRepository(path).list_prior_context_cards(
        "DEMO", target_date, target_cutoff,
    )
    assert first == second
    assert [str(item.id) for item in first] == [str(card.id) for card in cards]
    assert [item.content_sha256 for item in first] == [card.content_sha256 for card in cards]

    future_path = tmp_path / "future.sqlite"
    future_repo = memory.SQLiteResearchMemoryRepository(future_path)
    future_repo.save_company(company)
    for event_date, event_id in (
        (date(2026, 6, 24), "DEMO:2026-06-24:2026Q4"),
        (date(2026, 3, 18), "DEMO:2026-03-18:2026Q3"),
        (date(2026, 10, 1), "DEMO:2026-10-01:2027Q1"),
    ):
        _save_approved_prior_context_card(future_repo, company=company, event_date=event_date, external_event_id=event_id)
    del future_repo
    eligible = memory.SQLiteResearchMemoryRepository(
        future_path
    ).list_prior_context_cards("DEMO", target_date, target_cutoff)
    assert len(eligible) == 2
    assert all(item.event_date < target_date for item in eligible if item.event_date)



def test_memory_public_api_imports_key_contracts() -> None:
    assert memory.CompanyRecord.__name__ == "CompanyRecord"
    assert memory.SourcePassageRecord.__name__ == "SourcePassageRecord"
    assert memory.RecommendationAction.TRIM.value == "TRIM"
    assert memory.SupabaseEvidenceStore.__name__ == "SupabaseEvidenceStore"


def test_source_passage_is_saved_and_rehydrated_with_checksum(tmp_path: Path) -> None:
    r = _records()
    repo = memory.SQLiteResearchMemoryRepository(tmp_path / "memory.sqlite")
    repo.save_company(r["company"])
    repo.save_source(r["source"])
    repo.save_event(r["event"], source_links=(memory.EventSourceLink(source_id=r["source"].id, relationship="primary_filing", is_primary=True),))
    repo.save_session(r["session"])
    text = "cloud cloud capacity language"
    passage = memory.SourcePassageRecord(
        id=uuid4(),
        source_id=r["source"].id,
        passage_id="p1",
        ordinal=0,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        char_start=0,
        char_end=len(text),
        token_estimate=7,
        extraction_version="test-v1",
        created_at=NOW,
    )

    repo.save_source_passage(passage)

    bundle = repo.load_session_bundle(r["session"].id)
    assert bundle.source_passages == (passage,)






def test_source_passage_checksum_must_match_text() -> None:
    r = _records()

    with pytest.raises(ValueError, match="checksum does not match text"):
        memory.SourcePassageRecord(
            id=uuid4(),
            source_id=r["source"].id,
            passage_id="bad",
            ordinal=0,
            text="cloud capacity",
            text_sha256="b" * 64,
            char_start=0,
            char_end=14,
            token_estimate=4,
            extraction_version="test-source-passage-v1",
            created_at=NOW,
        )


def test_local_repository_rejects_post_cutoff_evidence_before_saving_draft(
    tmp_path: Path,
) -> None:
    r = _records()
    late_source = r["source"].model_copy(
        update={
            "id": uuid4(),
            "external_source_id": "late-news",
            "available_at": CUTOFF + timedelta(days=1),
            "published_at": CUTOFF + timedelta(days=1),
        }
    )
    repo = memory.SQLiteResearchMemoryRepository(tmp_path / "memory.sqlite")
    repo.save_company(r["company"])
    repo.save_source(late_source)
    repo.save_event(r["event"])
    repo.save_metric(r["metric"])
    repo.save_session(r["session"])
    link = memory.EvidenceLink(
        source_id=late_source.id,
        passage_id="late",
        relationship="context",
        available_at=late_source.available_at,
    )

    with pytest.raises(memory.MemoryRepositoryError, match="unavailable at cutoff"):
        repo.save_snapshot(r["snapshot"], evidence_links=(link,), metric_ids=(r["metric"].id,))

    fresh = memory.SQLiteResearchMemoryRepository(tmp_path / "memory.sqlite")
    assert fresh.load_session_bundle(r["session"].id).snapshots == ()














def test_postgrest_mapping_uses_only_migration_columns_and_errors_hide_secret() -> None:
    company = _records()["company"]
    row = _to_postgrest_row(company)
    assert set(row) == set(memory.CompanyRecord.model_fields)

    secret = "server-secret-that-must-not-leak"
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"echo": request.headers.get("authorization")})))
    repo = memory.SupabaseResearchMemoryRepository(url="https://project.example.test", secret_key=secret, client=client)
    with pytest.raises(memory.MemoryRepositoryError) as captured:
        repo.save_company(company)
    assert secret not in str(captured.value)
    assert secret not in repr(repo)


def test_supabase_existing_draft_finalizes_with_patch_not_insert_upsert() -> None:
    hypothesis = _records()["hypothesis"]
    locked = hypothesis.model_copy(
        update={
            "status": memory.HypothesisStatus.LOCKED,
            "approved_at": NOW,
            "approved_by": "analyst",
        }
    )
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[hypothesis.model_dump(mode="json")])
        return httpx.Response(204)

    repo = memory.SupabaseResearchMemoryRepository(
        url="https://project.example.test",
        secret_key="server-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    repo.save_hypothesis(locked)

    assert methods[-1] == "PATCH"
    assert "POST" not in methods


def test_supabase_prior_cards_scope_issuer_cutoff_and_projection() -> None:
    records = _records()
    company = records["company"]
    event = records["event"]
    card = memory.EventContextCardRecord(
        id=uuid4(), event_id=event.id, research_session_id=records["session"].id,
        snapshot_id=records["snapshot"].id, hypothesis_id=records["hypothesis"].id,
        hypothesis_assessment_id=records["assessment"].id, market_setup_id=records["setup"].id,
        market_reaction_id=records["reaction"].id, version=1, status="approved", data_kind="real",
        run_mode="replay", knowledge_cutoff_at=CUTOFF, replay_as_of_at=NOW,
        recommendation_action="DONT_BUY",
        payload={"human_readable": {"executive_summary": "Prior context"}}, content_sha256=SHA,
        prompt_version="v1", model_provider="fixture", model_name="deterministic",
        schema_version="1.0", actual_created_at=NOW, approved_at=NOW, approved_by="analyst",
        created_at=NOW, updated_at=NOW,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/companies"):
            return httpx.Response(200, json=[{"id": str(company.id), "ticker": "DEMO"}])
        if request.url.path.endswith("/earnings_events"):
            return httpx.Response(200, json=[{
                "id": str(event.id), "event_date": event.event_date.isoformat(),
                "external_event_id": event.external_event_id, "company_id": str(company.id),
            }])
        if request.url.path.endswith("/event_context_cards"):
            return httpx.Response(200, json=[card.model_dump(mode="json", exclude={"ticker", "event_date", "external_event_id"})])
        raise AssertionError(request.url)

    repo = memory.SupabaseResearchMemoryRepository(
        url="https://project.example.test", secret_key="server-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = repo.list_prior_context_cards("DEMO", date(2026, 2, 1), NOW, limit=3)
    assert result and result[0].ticker == "DEMO"
    assert result[0].external_event_id == event.external_event_id
    assert "ticker=eq.DEMO" in str(requests[0].url)
    assert "company_id=in." in str(requests[1].url)
    assert "event_id=in." in str(requests[2].url)
    assert "status=eq.approved" in str(requests[2].url)
    assert "knowledge_cutoff_at=lte." in str(requests[2].url)
    assert "limit=48" in str(requests[2].url)


def test_postgrest_mapping_excludes_joined_card_projection_fields() -> None:
    records = _records()
    card = memory.EventContextCardRecord(
        id=uuid4(), event_id=records["event"].id, research_session_id=records["session"].id,
        snapshot_id=records["snapshot"].id, hypothesis_id=records["hypothesis"].id,
        hypothesis_assessment_id=records["assessment"].id, market_setup_id=records["setup"].id,
        market_reaction_id=records["reaction"].id, version=1, status="draft", data_kind="real",
        run_mode="replay", knowledge_cutoff_at=CUTOFF, replay_as_of_at=NOW,
        payload={"card": "test"}, content_sha256=SHA, prompt_version="v1",
        model_provider="fixture", model_name="deterministic", schema_version="1.0",
        actual_created_at=NOW, created_at=NOW, updated_at=NOW,
        ticker="DEMO", event_date=date(2025, 12, 17), external_event_id="DEMO:2025-12-17:2026Q1",
    )
    row = _to_postgrest_row(card)
    assert "ticker" not in row
    assert "event_date" not in row
    assert "external_event_id" not in row


def test_supabase_lineage_round_trips_and_legacy_rows_default_empty() -> None:
    """New writes carry lineage; pre-migration rows remain readable."""
    records = _records()
    lineage = (
        memory.HistoricalContextLineageEntry(
            card_id=str(uuid4()),
            event_id="DEMO:2025-10-29:2026Q1",
            ticker="DEMO",
            event_date=date(2025, 10, 29),
            knowledge_cutoff_at=CUTOFF - timedelta(days=1),
            content_sha256="b" * 64,
        ),
    )
    snapshot = records["snapshot"].model_copy(
        update={
            "historical_context_manifest": lineage,
            "historical_context_manifest_sha256": (
                memory.historical_context_manifest_sha256(lineage)
            ),
        }
    )
    outbound = _to_postgrest_row(snapshot)
    assert outbound["historical_context_manifest"] == [
        lineage[0].model_dump(mode="json")
    ]
    assert outbound["historical_context_manifest_sha256"] == (
        memory.historical_context_manifest_sha256(lineage)
    )

    # A hosted project that has not yet applied the forward-only migration
    # will omit both columns.  The adapter must still read its old snapshots.
    legacy_row = snapshot.model_dump(mode="json")
    legacy_row.pop("historical_context_manifest")
    legacy_row.pop("historical_context_manifest_sha256")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/pre_event_snapshots")
        return httpx.Response(200, json=[legacy_row])

    repo = memory.SupabaseResearchMemoryRepository(
        url="https://project.example.test",
        secret_key="server-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    loaded = repo._many(
        "pre_event_snapshots", "research_session_id", snapshot.research_session_id
    )
    assert loaded == [
        snapshot.model_copy(
            update={
                "historical_context_manifest": (),
                "historical_context_manifest_sha256": None,
            }
        )
    ]


def test_supabase_evidence_store_uses_private_content_addressed_object_path() -> None:
    content = b"demo filing body"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=content if request.method == "GET" else b"{}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = memory.SupabaseEvidenceStore(
        url="https://project.example.test",
        secret_key="server-secret",
        client=client,
    )

    reference, digest, size = store.put_bytes(content, media_type="text/plain")
    assert reference == f"supabase-storage://eventlens-evidence/sha256/{digest}"
    assert digest == hashlib.sha256(content).hexdigest()
    assert size == len(content)
    assert requests[-1].url.path.endswith(f"/eventlens-evidence/sha256/{digest}")
    assert requests[-1].headers["x-upsert"] == "false"
    assert store.get_bytes(digest) == content


def test_supabase_evidence_store_treats_existing_matching_object_as_success() -> None:
    content = b"already uploaded"
    digest = hashlib.sha256(content).hexdigest()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, content=content)
        return httpx.Response(409, json={"message": "already exists"})

    store = memory.SupabaseEvidenceStore(
        url="https://project.example.test",
        secret_key="server-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    reference, uploaded_digest, size = store.put_bytes(content, media_type="text/plain")
    assert reference == f"supabase-storage://eventlens-evidence/sha256/{digest}"
    assert uploaded_digest == digest
    assert size == len(content)
    assert calls == ["POST", "GET"]


def test_memory_settings_are_secret_safe() -> None:
    settings = EventLensSettings.load(environment={
        "EVENTLENS_MEMORY_BACKEND": "supabase",
        "EVENTLENS_SUPABASE_URL": "https://project.example.test",
        "EVENTLENS_SUPABASE_SECRET_KEY": "server-secret",
        "EVENTLENS_SUPABASE_REQUEST_TIMEOUT_SECONDS": "12",
    })
    assert settings.memory_backend_status().configured
    assert settings.supabase_request_timeout_seconds == 12
    assert "server-secret" not in str(settings)
