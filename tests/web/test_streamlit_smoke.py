from pathlib import Path
import json
import time

from streamlit.testing.v1 import AppTest
from eventlens.config import EventLensSettings
from eventlens.web.streamlit_app import _settings_for_mode


REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "src/eventlens/web/streamlit_app.py"


def _finish_background(app: AppTest) -> AppTest:
    for _ in range(30):
        refresh = [button for button in app.button if button.label == "Refresh progress"]
        if not refresh:
            return app
        time.sleep(0.05)
        refresh[0].click().run()
    raise AssertionError("background Streamlit action did not finish")


def test_fixture_ui_smoke_runs_key_free_d0_product_journey(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EVENTLENS_WORKFLOW_ARTIFACT_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    assert "Research with a memory." in [item.value for item in app.title]
    assert any("Key-free D0 replay" in item.value for item in app.info)
    app.button[0].click().run()
    _finish_background(app)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Overview",
        "Prior context",
        "Deterministic baseline",
        "External handoff",
        "Artifacts",
        "Unavailable stages",
    ]
    assert len([item for item in app.success if "Context Card" in item.value]) == 3
    assert any("Baseline" in item.value for item in app.success)
    rendered_json = json.dumps([item.value for item in app.json], default=str)
    assert str(tmp_path) not in rendered_json


def test_d0_ui_import_and_fresh_app_resume(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVENTLENS_WORKFLOW_ARTIFACT_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.button[0].click().run()
    _finish_background(app)

    report = (REPO_ROOT / "fixtures/d0/demo/report.json").read_bytes()
    app.get("file_uploader")[0].upload(
        "report.json", report, "application/json"
    ).run()
    next(
        button for button in app.button
        if button.label == "Validate and import report"
    ).click().run()
    _finish_background(app)
    assert not app.exception
    assert any(
        "validated" in item.value.casefold()
        for item in app.success
    )

    reopened = AppTest.from_file(str(APP), default_timeout=15)
    reopened.session_state["session_id"] = "d0-fixture-demo-2026q3"
    reopened.run()
    next(
        button for button in reopened.button
        if button.label == "Resume durable session"
    ).click().run()
    _finish_background(reopened)

    assert not reopened.exception
    assert [tab.label for tab in reopened.tabs] == [
        "Overview",
        "Prior context",
        "Deterministic baseline",
        "External handoff",
        "Artifacts",
        "Unavailable stages",
    ]
    assert any(
        metric.label == "Stage" and metric.value == "Validated"
        for metric in reopened.metric
    )


def test_d0_ui_surfaces_invalid_import_on_completion(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EVENTLENS_WORKFLOW_ARTIFACT_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.button[0].click().run()
    _finish_background(app)
    app.get("file_uploader")[0].upload(
        "invalid.json", b"not-json", "application/json"
    ).run()
    next(
        button for button in app.button
        if button.label == "Validate and import report"
    ).click().run()
    _finish_background(app)

    assert any("report_invalid" in item.value for item in app.error)


def test_visible_mode_is_bound_to_backend_settings() -> None:
    base = EventLensSettings.model_validate({"data_mode": "fixture"})
    assert _settings_for_mode(base, "Fixture").data_mode == "fixture"
    assert _settings_for_mode(base, "Live").data_mode == "live"


def test_streamlit_shell_does_not_run_workflows_on_its_script_thread() -> None:
    source = APP.read_text(encoding="utf-8")
    assert "asyncio.run" not in source
    assert "Refresh progress" in source
    assert "ProductSessionFacade" in source
    assert "WebWorkflowService" not in source
