from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from eventlens.agents import ExternalAgentHandoffStore
from eventlens.config import AgentRoute, EventLensSettings
from eventlens.product.fixture_replay import prepare_d0_demo_fixture_replay
from eventlens.web.handoff import (
    HandoffFailureCode,
    SessionHandoffFacade,
)


def _prepared(tmp_path: Path):
    preparation = prepare_d0_demo_fixture_replay(tmp_path)
    return preparation, SessionHandoffFacade(tmp_path)


def test_facade_exposes_verified_bundle_download_and_distinct_checksums(tmp_path: Path) -> None:
    preparation, facade = _prepared(tmp_path)
    session_id = preparation.package.manifest.session_id

    view = facade.view(session_id)

    assert view.status == "awaiting_external_agent"
    assert view.route is AgentRoute.EXTERNAL
    assert view.upload_enabled is True
    assert view.checksums is not None
    assert view.checksums.content_sha256 == preparation.package.bundle.content_sha256
    assert view.checksums.file_sha256 == hashlib.sha256(preparation.package.bundle_bytes).hexdigest()
    assert view.checksums.content_sha256 != view.checksums.file_sha256
    assert view.bundle_download is not None
    assert view.bundle_download.content == preparation.package.bundle_bytes
    assert not hasattr(view, "bundle_path")
    assert str(tmp_path) not in view.model_dump_json()


def test_facade_imports_valid_report_idempotently_and_maps_invalid_upload(tmp_path: Path) -> None:
    preparation, facade = _prepared(tmp_path)
    session_id = preparation.package.manifest.session_id

    invalid = facade.import_report(session_id, b"not json")
    assert invalid.ok is False
    assert invalid.failure is not None
    assert invalid.failure.code is HandoffFailureCode.REPORT_INVALID
    assert facade.view(session_id).status == "awaiting_external_agent"

    imported = facade.import_report(session_id, preparation.package.report_bytes)
    assert imported.ok is True
    assert imported.idempotent is False
    retried = facade.import_report(session_id, preparation.package.report_bytes)
    assert retried.ok is True
    assert retried.idempotent is True
    assert facade.view(session_id).status == "validated"


def test_wrong_bundle_and_conflicting_report_remain_safe(tmp_path: Path) -> None:
    preparation, facade = _prepared(tmp_path)
    session_id = preparation.package.manifest.session_id
    wrong = json.loads(preparation.package.report_bytes)
    wrong["bundle_sha256"] = "a" * 64
    rejected = facade.import_report(session_id, json.dumps(wrong))
    assert rejected.failure is not None
    assert rejected.failure.code is HandoffFailureCode.REPORT_INVALID
    assert facade.view(session_id).status == "awaiting_external_agent"

    first = facade.import_report(session_id, preparation.package.report_bytes)
    assert first.ok
    conflicting = json.loads(preparation.package.report_bytes)
    conflicting["report_id"] = "different-report"
    conflict = facade.import_report(session_id, json.dumps(conflicting))
    assert conflict.ok is False
    assert conflict.conflict is True
    assert conflict.failure is not None
    assert conflict.failure.code is HandoffFailureCode.CONFLICT
    assert facade.load_state(session_id).report_id == preparation.package.report.report_id


def test_unsafe_session_ids_are_rejected_without_path_access(tmp_path: Path) -> None:
    facade = SessionHandoffFacade(tmp_path)
    for session_id in ("../escape", "a/b", "", ".", "session id", "a\\b"):
        view = facade.view(session_id)
        assert view.failure is not None
        assert view.failure.code is HandoffFailureCode.INVALID_SESSION_ID
        result = facade.import_report(session_id, b"{}")
        assert result.failure is not None
        assert result.failure.code is HandoffFailureCode.INVALID_SESSION_ID


def test_deepseek_view_is_ready_or_explicitly_not_ready_and_disables_upload(tmp_path: Path) -> None:
    settings = EventLensSettings(external_agent_enabled=False)
    facade = SessionHandoffFacade(tmp_path, settings=settings)
    view = facade.view("deepseek-session")

    assert view.route is AgentRoute.DEEPSEEK
    assert view.upload_enabled is False
    assert view.failure is not None
    assert view.failure.code is HandoffFailureCode.NOT_FOUND
    disabled = facade.import_report("deepseek-session", b"{}")
    assert disabled.failure is not None
    # A missing session is reported before route mutation is possible; a
    # persisted DeepSeek state is covered by the route guard in the façade.
    assert disabled.failure.code is HandoffFailureCode.NOT_FOUND


def test_tampered_bundle_is_not_downloadable_and_state_remains_exact(tmp_path: Path) -> None:
    preparation, facade = _prepared(tmp_path)
    session_id = preparation.package.manifest.session_id
    state = facade.load_state(session_id)
    Path(state.bundle_path).write_bytes(b"tampered")

    view = facade.view(session_id)
    assert view.failure is not None
    assert view.failure.code is HandoffFailureCode.BUNDLE_CHECKSUM_MISMATCH
    with pytest.raises(ValueError, match="verified"):
        facade.bundle_download(session_id)
    assert ExternalAgentHandoffStore(tmp_path).load(session_id).status.value == "awaiting_external_agent"
