from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCANNER = Path(__file__).resolve().parents[1] / "scripts" / "check_public_release.py"


def make_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "check_public_release.py").write_bytes(SCANNER.read_bytes())
    (root / "README.md").write_text("Synthetic public project.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def run_scan(root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "check_public_release.py")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_public_release_scan_passes_clean_temporary_repository(tmp_path: Path) -> None:
    result = run_scan(make_repository(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Working-tree public-release scan passed" in result.stdout
    assert "does not inspect prior commits, prove clean history" in result.stdout


def test_public_release_scan_rejects_secret_in_tracked_sql(tmp_path: Path) -> None:
    root = make_repository(tmp_path)
    sql = root / "supabase" / "migrations" / "secret.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text(f"select '{'AKIA' + 'A' * 16}';\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", str(sql)], check=True)

    result = run_scan(root)

    assert result.returncode == 1
    assert "secret.sql: resembles AWS access key" in result.stdout


def test_public_release_scan_rejects_tracked_non_example_env(tmp_path: Path) -> None:
    root = make_repository(tmp_path)
    env_file = root / ".env"
    env_file.write_text("SETTING=synthetic\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-f", str(env_file)], check=True)

    result = run_scan(root)

    assert result.returncode == 1
    assert ".env: environment file must not be tracked" in result.stdout
