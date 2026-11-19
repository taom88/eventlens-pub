from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import os
from zipfile import ZipFile


def test_wheel_contains_public_runtime_assets_and_runs_demo_from_empty_directory(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
            "--no-build-isolation", "--wheel-dir", str(tmp_path),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("eventlens-*.whl"))
    with ZipFile(wheel) as archive:
        vintage_assets = {
            name for name in archive.namelist()
            if name.startswith("eventlens/ontologies/vintages/")
            and name.endswith(".yaml")
        }
        assets = set(archive.namelist())
        license_files = {
            name for name in assets
            if name.endswith(".dist-info/licenses/LICENSE")
            or name.endswith(".dist-info/licenses/NOTICE")
        }

    assert vintage_assets == {
        "eventlens/ontologies/vintages/sector.internet_platform/1.0.0.yaml",
        "eventlens/ontologies/vintages/sector.semiconductors/1.0.0.yaml",
        "eventlens/ontologies/vintages/universal/1.0.0.yaml",
    }
    assert "eventlens/schemas/eventlens.external_agent.v1.schema.json" in assets
    assert "eventlens/fixtures/d0/demo/manifest.json" in assets
    assert "eventlens/fixtures/DEMO.json" in assets
    assert "eventlens/fixtures/d0/demo/report.json" in assets
    assert "eventlens/fixtures/external_agent/v1/cases.json" in assets
    assert "eventlens/docs/external-agent-operator.md" in assets
    assert license_files == {
        "eventlens-0.1.0.dist-info/licenses/LICENSE",
        "eventlens-0.1.0.dist-info/licenses/NOTICE",
    }

    install_root = tmp_path / "installed"
    install_root.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(install_root), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    empty_cwd = tmp_path / "empty-cwd"
    empty_cwd.mkdir()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(install_root)
    environment.pop("EVENTLENS_FIXTURE_DIR", None)
    completed = subprocess.run(
        [sys.executable, "-c", "from eventlens.app.commands import main; raise SystemExit(main(['demo']))"],
        cwd=empty_cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"status": "validated"' in completed.stdout
    assert '"provider_calls": 0' in completed.stdout
    assert '"model_calls": 0' in completed.stdout
