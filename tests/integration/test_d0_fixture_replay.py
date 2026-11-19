from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from eventlens.agents import DeterministicExternalAgentFixture
from eventlens.product.fixture_replay import (
    DEFAULT_FIXTURE_ROOT,
    FixtureReplayError,
    load_d0_demo_fixture,
    prepare_d0_demo_fixture_replay,
    run_d0_demo_fixture_replay,
)
from eventlens.product.fixture_replay import FixturePreEventFeatures, FixtureReactionMetrics
from eventlens.web.context_baseline import load_context_cards
from eventlens.web.handoff import SessionHandoffFacade
from eventlens.web.product_session import ProductSessionFacade


def test_d0_fixture_has_exactly_three_readable_cards_and_fixed_baseline() -> None:
    package = load_d0_demo_fixture()

    assert len(package.cards) == 3
    for card in package.cards:
        assert card.executive_summary
        assert card.key_messages
        assert card.metric_interpretation
        assert card.data_quality_caveats
        assert card.provenance_links
        assert card.origin == "fixture_replay"
    baseline = package.baseline
    assert baseline.sample_size == 3
    assert baseline.signed_move_pct == -baseline.absolute_move_pct
    assert baseline.confidence_interval is None
    assert baseline.confidence_interval_missing_reason
    assert baseline.guardrails_are_not_valuation is True
    assert all("not valuation" in guardrail.basis for guardrail in baseline.mechanical_guardrails)


def test_d0_cards_expose_canonical_reactions_and_cutoff_features_with_missingness() -> None:
    package = load_d0_demo_fixture()
    view = load_context_cards([card.model_dump(mode="json") for card in package.cards])

    assert len(view.cards) == 3
    for card in view.cards:
        assert card.pre_event_features.prior_5d_return_pct is not None
        assert card.pre_event_features.prior_20d_return_pct is not None
        assert card.pre_event_features.recency_weighted_sentiment is None
        assert any(item.field == "recency_weighted_sentiment" for item in card.pre_event_features.missing_values)
        assert card.reaction_metrics.next_session_open_vs_reference_close_pct is not None
        assert card.reaction_metrics.absolute_next_session_open_move_pct == abs(
            card.reaction_metrics.next_session_open_vs_reference_close_pct
        )
        assert any(item.field == "after_hours_return_pct" for item in card.reaction_metrics.missing_values)
        assert any(item.field == "after_hours_return_pct" for item in card.missing_values)


def test_d0_metric_schema_rejects_unexplained_nulls_and_mismatched_absolute_move() -> None:
    with pytest.raises(ValueError, match="missing pre-event feature"):
        FixturePreEventFeatures(
            article_count=0,
            source_count=0,
            mean_ticker_relevance=None,
            weighted_sentiment=None,
            recency_weighted_sentiment=None,
            sentiment_positive_share=None,
            latest_close="1",
            prior_5d_return_pct="0",
            prior_20d_return_pct="0",
        )
    with pytest.raises(ValueError, match="absolute next-open move"):
        FixtureReactionMetrics(
            after_hours_return_pct=None,
            next_session_open_vs_reference_close_pct="-1",
            t_plus_1_close_return_pct="0",
            t_plus_5_close_return_pct="0",
            absolute_next_session_open_move_pct="0",
            missing_values=(
                {"field": "after_hours_return_pct", "reason": "not retained"},
            ),
        )


def test_d0_replay_never_invokes_provider_or_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("fixture replay must not invoke a provider or model")

    monkeypatch.setattr("eventlens.providers.registry.build_free_first_registry", fail)
    monkeypatch.setattr(DeterministicExternalAgentFixture, "interpret", fail)
    run = run_d0_demo_fixture_replay(tmp_path / "handoff")

    assert run.package.manifest.provider_calls == 0
    assert run.package.manifest.model_calls == 0
    assert run.imported.status.value == "validated"


def test_d0_bundle_and_report_checksums_are_stable_and_bound() -> None:
    first = load_d0_demo_fixture()
    second = load_d0_demo_fixture()

    assert first.bundle_bytes == second.bundle_bytes
    assert first.report_bytes == second.report_bytes
    assert first.bundle.content_sha256 == second.bundle.content_sha256
    assert hashlib.sha256(first.bundle_bytes).hexdigest() == first.manifest.checksums["bundle.json"]
    assert hashlib.sha256(first.report_bytes).hexdigest() == first.manifest.checksums["report.json"]
    assert first.report.bundle_sha256 == first.bundle.content_sha256
    assert first.report.bundle_id == first.bundle.bundle_id


def test_d0_card_knowledge_cutoffs_follow_outcomes_and_precede_target() -> None:
    package = load_d0_demo_fixture()
    target_cutoff = package.bundle.information_cutoff_at

    for card, evidence, history in zip(
        package.cards, package.bundle.evidence, package.bundle.historical_context
    ):
        assert card.outcome_available_at.tzinfo is not None
        assert card.knowledge_cutoff_at.tzinfo is not None
        assert card.knowledge_cutoff_at > card.outcome_available_at
        assert card.knowledge_cutoff_at < target_cutoff
        assert card.available_at == card.knowledge_cutoff_at
        assert evidence.available_at == card.knowledge_cutoff_at
        assert history.knowledge_cutoff_at == card.knowledge_cutoff_at


def test_d0_prepare_export_pauses_before_explicit_import(tmp_path: Path) -> None:
    preparation = prepare_d0_demo_fixture_replay(tmp_path / "handoff")

    assert preparation.exported.status.value == "awaiting_external_agent"
    assert preparation.exported.report_id is None
    assert preparation.exported.report_path is None


def test_d0_export_import_and_new_store_instance_preserve_identity(tmp_path: Path) -> None:
    run = run_d0_demo_fixture_replay(tmp_path / "handoff")

    assert run.exported.bundle_id == run.imported.bundle_id == run.reopened.bundle_id
    assert run.imported.report_id == run.reopened.report_id == run.package.report.report_id
    assert run.imported.bundle_sha256 == run.package.bundle.content_sha256
    assert run.imported.report_sha256
    assert run.imported.report_path
    assert run.reopened.model_dump(mode="json") == run.imported.model_dump(mode="json")


def test_d0_product_session_rehydrates_in_a_fresh_process(tmp_path: Path) -> None:
    root = tmp_path / "product"
    service = ProductSessionFacade(session_root=root)
    session = service.submit("Prepare DEMO earnings research", "fixture_replay")
    package = load_d0_demo_fixture()
    imported = SessionHandoffFacade(service.fixture_handoff_root).import_report(
        session.session_id, package.report_bytes
    )
    assert imported.ok

    script = (
        "import json,sys; "
        "from pathlib import Path; "
        "from eventlens.web.product_session import ProductSessionFacade; "
        "s=ProductSessionFacade(session_root=Path(sys.argv[1])).load(sys.argv[2]); "
        "print(json.dumps({'session_id':s.session_id,'status':s.status,"
        "'cards':len(s.context_refs),'origin':s.origin}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root), session.session_id],
        cwd=Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "session_id": session.session_id,
        "status": "validated",
        "cards": 3,
        "origin": "fixture_replay",
    }


def test_d0_future_stages_are_explicitly_unavailable() -> None:
    package = load_d0_demo_fixture()

    assert set(package.manifest.unavailable_stages) == set(package.bundle.unavailable_stages)
    assert package.bundle.unavailable_stages
    assert all(stage not in package.report.warnings for stage in package.bundle.unavailable_stages)


def test_d0_tampered_fixture_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    for source in DEFAULT_FIXTURE_ROOT.rglob("*"):
        if source.is_file():
            destination = root / source.relative_to(DEFAULT_FIXTURE_ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
    card = root / "cards" / "demo-d0-q2-2025.json"
    card.write_bytes(card.read_bytes().replace(b"mixed setup", b"tampered setup"))

    with pytest.raises(FixtureReplayError, match="checksum"):
        load_d0_demo_fixture(root)
