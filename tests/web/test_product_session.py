from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

import pytest

from eventlens.config import EventLensSettings
from eventlens.memory import (
    ImmutableRevisionError,
    RunRecordKind,
    SQLiteResearchMemoryRepository,
    canonical_run_record_checksum,
)
from eventlens.product.ask import ResearchAskResult
from eventlens.web.handoff import SessionHandoffFacade
from eventlens.web.product_session import (
    ProductSessionCollisionError,
    ProductSessionFacade,
    ProductSessionInputError,
    ProductSessionStaleRevisionError,
    SessionMode,
    _explicit_origin,
    _product_run_id,
)
from eventlens.web.presentation import product_session_view
from tests.product.test_research_evidence import _bundle


class _EvidenceSource:
    def build(self, *, ticker: str, question: str, session_id: str):
        bundle = _bundle()
        assert ticker == bundle.target.ticker
        return bundle


def test_fixture_submit_pauses_without_provider_or_model_calls(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("fixture replay attempted a live/model call")

    monkeypatch.setattr("eventlens.web.product_session.product_run.execute_research_ask", forbidden)
    service = ProductSessionFacade(session_root=tmp_path)
    session = service.submit(
        "Prepare DEMO earnings research",
        SessionMode.FIXTURE_REPLAY,
    )

    assert session.origin == "fixture_replay"
    assert session.status == "awaiting_external_agent"
    assert session.readiness.ready
    assert len(session.context_refs) == 3
    assert session.baseline_ref is not None
    assert session.handoff is not None
    view = product_session_view(session)
    assert view.run_mode == "fixture_replay"
    assert view.provider_origin == "fixture_replay"
    assert view.event_id == "DEMO:2026-10-29:2026Q3"
    assert view.cutoff_at is not None
    assert view.persistence_backend == "local_product_store"
    assert view.prior_card_coverage.available == 3
    assert view.awaiting_agent
    assert view.unavailable_future_stages


def test_fixture_submit_is_idempotent_and_reloads_after_restart(tmp_path: Path) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    first = service.submit("Prepare DEMO earnings research", "fixture_replay")
    duplicate = service.submit("Prepare DEMO earnings research", "fixture_replay")
    reopened = ProductSessionFacade(session_root=tmp_path).load(first.session_id)

    assert duplicate.session_id == first.session_id
    assert duplicate.idempotency_key == first.idempotency_key
    assert reopened.status == first.status
    assert tuple(item.sha256 for item in reopened.artifact_refs) == tuple(
        item.sha256 for item in first.artifact_refs
    )


def test_product_session_repository_is_authoritative_after_mirror_removal(tmp_path: Path) -> None:
    settings = EventLensSettings(
        memory_backend="local", memory_sqlite_path=tmp_path / "memory.sqlite",
        memory_owner_id="owner-test", memory_workspace_id="workspace-test",
    )
    first = ProductSessionFacade(settings, session_root=tmp_path / "artifacts").submit(
        "Prepare DEMO earnings research", "fixture_replay"
    )
    session_file = tmp_path / "artifacts" / "sessions" / f"{first.session_id}.json"
    session_file.unlink()
    session_file.with_suffix(".json.sha256").unlink()

    reopened = ProductSessionFacade(settings, session_root=tmp_path / "artifacts").load(
        first.session_id
    )
    assert reopened == first
    run_id = _product_run_id(
        first.session_id,
        owner_id=settings.memory_owner_id,
        workspace_id=settings.memory_workspace_id,
    )
    records = SQLiteResearchMemoryRepository(settings.memory_sqlite_path).list_run_records(
        "workspace-test", run_id, kind=RunRecordKind.TRANSITION
    )
    assert records
    latest = records[-1]
    assert latest.owner_id == "owner-test"
    assert latest.metadata["product_session"] == first.model_dump(mode="json")
    session_sha256 = hashlib.sha256(
        json.dumps(first.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    assert latest.metadata["product_session_sha256"] == session_sha256
    assert latest.checksum == canonical_run_record_checksum(latest)


def test_stale_product_revision_fails_closed(tmp_path: Path) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    session = service.submit("Prepare DEMO earnings research", "fixture_replay")

    with pytest.raises(ProductSessionStaleRevisionError):
        service._persist(session, expected_revision=0)


def test_concurrent_revision_insert_is_a_typed_stale_failure(
    tmp_path: Path, monkeypatch
) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    session = service.submit("Prepare DEMO earnings research", "fixture_replay")

    def concurrent_insert(record):
        raise ImmutableRevisionError("simulated concurrent insert")

    monkeypatch.setattr(service.repository, "save_run_record", concurrent_insert)
    with pytest.raises(ProductSessionStaleRevisionError):
        service._persist(session, expected_revision=1)


def test_supabase_facade_never_uses_local_session_mirror_as_source(tmp_path: Path, monkeypatch) -> None:
    local = ProductSessionFacade(session_root=tmp_path)
    session = local.submit("Prepare DEMO earnings research", "fixture_replay")
    settings = EventLensSettings(
        memory_backend="supabase", supabase_url="https://user-project.test",
        supabase_secret_key="server-only-test-key", memory_owner_id="owner",
        memory_workspace_id="workspace",
    )

    class EmptyRepository:
        def list_run_records(self, workspace_id, run_id, *, kind=None):
            assert workspace_id == "workspace"
            return ()

    monkeypatch.setattr("eventlens.web.product_session.repository_from_settings", lambda selected: EmptyRepository())
    hosted = ProductSessionFacade(settings, session_root=tmp_path)
    from eventlens.web.product_session import ProductSessionNotFoundError
    with pytest.raises(ProductSessionNotFoundError):
        hosted.load(session.session_id)


def test_product_session_uses_configured_supabase_repository(tmp_path: Path, monkeypatch) -> None:
    from eventlens.memory import factory as memory_factory

    sentinel = object()
    received = {}

    def hosted_repository(**kwargs):
        received.update(kwargs)
        return sentinel

    monkeypatch.setattr(memory_factory, "SupabaseResearchMemoryRepository", hosted_repository)
    settings = EventLensSettings(
        memory_backend="supabase", supabase_url="https://personal-project.test",
        supabase_secret_key="server-only-test-key", memory_owner_id="owner-x",
        memory_workspace_id="workspace-y",
    )
    facade = ProductSessionFacade(settings, session_root=tmp_path)
    assert facade.repository is sentinel
    assert received["url"] == "https://personal-project.test"
    assert received["secret_key"] == settings.supabase_secret_key


def test_memory_owner_workspace_environment_settings_are_applied() -> None:
    settings = EventLensSettings.load(environment={
        "EVENTLENS_MEMORY_OWNER_ID": "analyst-7",
        "EVENTLENS_MEMORY_WORKSPACE_ID": "portfolio-research",
    })
    assert settings.memory_owner_id == "analyst-7"
    assert settings.memory_workspace_id == "portfolio-research"


def test_fixture_uses_canonical_key_across_browser_restarts(tmp_path: Path) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    first = service.submit(
        "Prepare DEMO earnings research",
        "fixture_replay",
        idempotency_key="web:first-browser-tab",
    )
    second = service.submit(
        "Prepare DEMO earnings research",
        "fixture_replay",
        idempotency_key="web:replacement-browser-tab",
    )

    assert first == second
    assert first.idempotency_key == f"fixture:{first.session_id}"


def test_fixture_import_is_authoritative_after_restart(tmp_path: Path) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    session = service.submit("Prepare DEMO earnings research", "fixture_replay")
    from eventlens.product.fixture_replay import load_d0_demo_fixture

    package = load_d0_demo_fixture()
    imported = service.import_report(session.session_id, package.report_bytes)
    assert imported.ok

    run_id = _product_run_id(
        session.session_id,
        owner_id=service.settings.memory_owner_id,
        workspace_id=service.settings.memory_workspace_id,
    )
    latest = service.repository.list_run_records(
        service.settings.memory_workspace_id,
        run_id,
        kind=RunRecordKind.TRANSITION,
    )[-1]
    assert latest.metadata["product_session"]["status"] == "validated"

    reopened = ProductSessionFacade(session_root=tmp_path).load(session.session_id)
    assert reopened.status == "validated"
    assert reopened.handoff is not None
    assert any(item.kind == "handoff_report_markdown" for item in reopened.artifact_refs)


def test_handoff_commit_is_reconciled_after_repository_commit_crash(
    tmp_path: Path, monkeypatch
) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    session = service.submit("Prepare DEMO earnings research", "fixture_replay")
    from eventlens.product.fixture_replay import load_d0_demo_fixture

    package = load_d0_demo_fixture()
    persist = service._persist

    def crash_after_handoff(candidate, *, expected_revision):
        if candidate.status == "validated":
            raise SystemExit("simulated crash before repository commit")
        return persist(candidate, expected_revision=expected_revision)

    monkeypatch.setattr(service, "_persist", crash_after_handoff)
    with pytest.raises(SystemExit):
        service.import_report(session.session_id, package.report_bytes)

    reopened = ProductSessionFacade(session_root=tmp_path).load(session.session_id)
    assert reopened.status == "validated"
    assert reopened.handoff is not None
    assert reopened.handoff.report_id == package.report.report_id


def test_stale_local_handoff_cannot_roll_repository_backward(tmp_path: Path) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    session = service.submit("Prepare DEMO earnings research", "fixture_replay")
    state_path = (
        service.fixture_handoff_root / session.session_id / "handoff-state.json"
    )
    awaiting_state = state_path.read_bytes()
    from eventlens.product.fixture_replay import load_d0_demo_fixture

    package = load_d0_demo_fixture()
    assert SessionHandoffFacade(service.fixture_handoff_root).import_report(
        session.session_id, package.report_bytes
    ).ok
    assert service.load(session.session_id).status == "validated"

    state_path.write_bytes(awaiting_state)
    reopened = ProductSessionFacade(session_root=tmp_path).load(session.session_id)

    assert reopened.status == "validated"
    assert reopened.handoff is not None
    assert reopened.handoff.status.value == "validated"


def test_repeated_load_does_not_duplicate_handoff_refs_or_revisions(tmp_path: Path) -> None:
    service = ProductSessionFacade(session_root=tmp_path)
    session = service.submit("Prepare DEMO earnings research", "fixture_replay")
    from eventlens.product.fixture_replay import load_d0_demo_fixture

    package = load_d0_demo_fixture()
    assert SessionHandoffFacade(service.fixture_handoff_root).import_report(
        session.session_id, package.report_bytes
    ).ok

    first = service.load(session.session_id)
    run_id = _product_run_id(
        session.session_id,
        owner_id=service.settings.memory_owner_id,
        workspace_id=service.settings.memory_workspace_id,
    )
    revision_count = len(
        service.repository.list_run_records(
            service.settings.memory_workspace_id,
            run_id,
            kind=RunRecordKind.TRANSITION,
        )
    )
    for _ in range(50):
        reopened = ProductSessionFacade(session_root=tmp_path).load(session.session_id)
        assert reopened == first

    kinds = [item.kind for item in reopened.artifact_refs]
    assert kinds.count("handoff_report") == 1
    assert kinds.count("handoff_report_markdown") == 1
    assert len(
        service.repository.list_run_records(
            service.settings.memory_workspace_id,
            run_id,
            kind=RunRecordKind.TRANSITION,
        )
    ) == revision_count


def test_product_run_identity_is_scoped_by_owner_and_workspace(tmp_path: Path) -> None:
    database = tmp_path / "shared.sqlite"
    first_settings = EventLensSettings(
        memory_backend="local",
        memory_sqlite_path=database,
        memory_owner_id="owner-a",
        memory_workspace_id="workspace-a",
    )
    second_settings = first_settings.model_copy(
        update={"memory_owner_id": "owner-b", "memory_workspace_id": "workspace-b"}
    )

    first = ProductSessionFacade(
        first_settings, session_root=tmp_path / "first"
    ).submit("Prepare DEMO earnings research", "fixture_replay")
    second = ProductSessionFacade(
        second_settings, session_root=tmp_path / "second"
    ).submit("Prepare DEMO earnings research", "fixture_replay")

    assert first.session_id == second.session_id
    assert _product_run_id(
        first.session_id,
        owner_id=first_settings.memory_owner_id,
        workspace_id=first_settings.memory_workspace_id,
    ) != _product_run_id(
        second.session_id,
        owner_id=second_settings.memory_owner_id,
        workspace_id=second_settings.memory_workspace_id,
    )


def test_live_missing_provider_is_typed_and_never_claims_live_origin(tmp_path: Path) -> None:
    settings = EventLensSettings(
        data_mode="live",
        alpha_vantage_api_key=None,
        finnhub_api_key=None,
        enable_yfinance=False,
    )
    session = ProductSessionFacade(settings, session_root=tmp_path).submit(
        "Prepare DEMO earnings research", "live", idempotency_key="live-missing"
    )
    assert session.status == "error"
    assert session.stage == "provider_readiness"
    assert session.origin is None
    assert session.error is not None
    assert session.error.code == "live_provider_not_ready"


def test_live_readiness_requires_a_universal_research_evidence_source(
    tmp_path: Path,
) -> None:
    settings = EventLensSettings(
        data_mode="live",
        alpha_vantage_api_key=None,
        finnhub_api_key="configured-not-printed",
        enable_yfinance=True,
    )
    session = ProductSessionFacade(settings, session_root=tmp_path).submit(
        "Prepare DEMO earnings research", "live", idempotency_key="missing-source"
    )

    assert session.stage == "provider_readiness"
    assert session.readiness.required == ("research_evidence_source",)
    assert session.readiness.missing == ("research_evidence_source",)


def test_live_provider_intent_is_persisted_before_calls_and_not_replayed_after_crash(
    tmp_path: Path, monkeypatch
) -> None:
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(
        "eventlens.web.product_session.product_run.execute_research_ask", interrupted
    )
    settings = EventLensSettings(
        data_mode="live",
        alpha_vantage_api_key="configured-not-printed",
        finnhub_api_key="configured-not-printed",
        enable_yfinance=True,
    )
    first = ProductSessionFacade(settings, session_root=tmp_path, evidence_source=_EvidenceSource())
    with pytest.raises(SystemExit):
        first.submit(
            "Prepare DEMO earnings research",
            "live",
            idempotency_key="crash-safe-live-run",
        )

    import os
    original_pid = os.getpid()
    monkeypatch.setattr(
        "eventlens.web.product_session.os.getpid", lambda: original_pid + 1
    )
    restarted = ProductSessionFacade(settings, session_root=tmp_path, evidence_source=_EvidenceSource())
    reopened = restarted.submit(
        "Prepare DEMO earnings research",
        "live",
        idempotency_key="crash-safe-live-run",
    )
    assert calls == 1
    assert reopened.status == "interrupted"
    assert reopened.stage == "interrupted_provider_collection"
    assert reopened.origin is None
    assert "no provider calls were retried automatically" in reopened.warnings[0]


def test_live_origin_is_read_from_checksum_verified_card_provenance(
    tmp_path: Path,
) -> None:
    card_path = tmp_path / "card.json"
    raw = json.dumps(
        {"source_provenance": [{"origin": "live"}, {"origin": "live"}]},
        sort_keys=True,
    ).encode()
    card_path.write_bytes(raw)
    result = ResearchAskResult(
        question="Prepare Example Systems earnings research",
        ticker="DEMO",
        session_id="live-origin",
        status="awaiting_external_agent",
        stage="awaiting_external_agent",
        model_provider="external",
        model="user_supplied",
        cards=(
            {
                "event_id": "DEMO:event",
                "path": str(card_path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "status": "draft",
            },
        ),
        next_action="Import report",
        created_at=datetime.now(timezone.utc),
    )

    assert _explicit_origin(result) == "live"


@pytest.mark.parametrize("question", ("", "Prepare OTHER earnings research"))
def test_fixture_rejects_blank_or_unsupported_requests(tmp_path: Path, question: str) -> None:
    with pytest.raises(ProductSessionInputError):
        ProductSessionFacade(session_root=tmp_path).submit(question, "fixture_replay")


def test_live_cannot_claim_reserved_fixture_session(tmp_path: Path) -> None:
    with pytest.raises(ProductSessionCollisionError):
        ProductSessionFacade(session_root=tmp_path).submit(
            "Prepare DEMO earnings research",
            "live",
            session_id="d0-fixture-demo-2026q3",
        )
