from pathlib import Path

import pytest

from eventlens.config import EventLensSettings
from eventlens.product.run import execute_research_ask, load_research_run


def test_shared_research_use_case_persists_and_rehydrates(tmp_path: Path) -> None:
    record = execute_research_ask(
        "Prepare DEMO earnings research",
        settings=EventLensSettings(),
        output_dir=tmp_path,
        session_id="demo-product-run",
    )

    reopened = load_research_run(tmp_path, "demo-product-run")
    assert reopened.result == record.result
    assert reopened.artifacts.json_sha256 == record.artifacts.json_sha256
    assert reopened.artifacts.markdown_sha256 == record.artifacts.markdown_sha256


def test_rehydration_rejects_tampered_artifact(tmp_path: Path) -> None:
    record = execute_research_ask(
        "Prepare DEMO earnings research",
        settings=EventLensSettings(),
        output_dir=tmp_path,
        session_id="demo-tamper",
    )
    record.artifacts.json_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        load_research_run(tmp_path, "demo-tamper")


@pytest.mark.parametrize("session_id", ("../escape", "two/slashes", "", "."))
def test_research_use_case_rejects_unsafe_session_ids(
    tmp_path: Path, session_id: str
) -> None:
    with pytest.raises(ValueError, match="safe stable"):
        execute_research_ask(
            "Prepare DEMO earnings research",
            settings=EventLensSettings(),
            output_dir=tmp_path,
            session_id=session_id,
        )
