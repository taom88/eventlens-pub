"""Shared research-ask execution and checksum-verified local rehydration."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eventlens.config import EventLensSettings
from eventlens.live_preflight import write_preflight_artifact

from .ask import ResearchAskResult, run_research_ask, write_research_markdown
from .research_evidence import ResearchEvidenceBundle


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


class ResearchRunArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    json_path: Path
    json_sha256_path: Path
    markdown_path: Path
    markdown_sha256_path: Path
    json_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    markdown_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResearchRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result: ResearchAskResult
    artifacts: ResearchRunArtifacts


def execute_research_ask(
    question: str,
    *,
    settings: EventLensSettings,
    output_dir: Path,
    session_id: str | None = None,
    evidence_bundle: ResearchEvidenceBundle | None = None,
    ticker: str | None = None,
) -> ResearchRunRecord:
    """Execute the product use case and persist its canonical companions once."""

    if session_id is not None:
        _validate_session_id(session_id)
    result = run_research_ask(
        question,
        settings=settings,
        output_dir=output_dir,
        session_id=session_id,
        evidence_bundle=evidence_bundle,
        ticker=ticker,
    )
    _validate_session_id(result.session_id)
    payload = result.model_dump(mode="json")
    json_path = output_dir / f"{result.session_id}.json"
    _, json_sha_path, json_sha = write_preflight_artifact(payload, json_path)
    markdown_path = json_path.with_suffix(".md")
    _, markdown_sha_path, markdown_sha = write_research_markdown(payload, markdown_path)
    return ResearchRunRecord(
        result=result,
        artifacts=ResearchRunArtifacts(
            json_path=json_path,
            json_sha256_path=json_sha_path,
            markdown_path=markdown_path,
            markdown_sha256_path=markdown_sha_path,
            json_sha256=json_sha,
            markdown_sha256=markdown_sha,
        ),
    )


def load_research_run(output_dir: Path, session_id: str) -> ResearchRunRecord:
    """Rehydrate an exact local run and fail closed on missing/tampered artifacts."""

    _validate_session_id(session_id)
    json_path = output_dir / f"{session_id}.json"
    markdown_path = output_dir / f"{session_id}.md"
    json_sha_path = json_path.with_suffix(".json.sha256")
    markdown_sha_path = markdown_path.with_suffix(".md.sha256")
    json_sha = _verify_sidecar(json_path, json_sha_path)
    markdown_sha = _verify_sidecar(markdown_path, markdown_sha_path)
    result = ResearchAskResult.model_validate_json(json_path.read_bytes())
    if result.session_id != session_id:
        raise ValueError("research artifact session binding mismatch")
    return ResearchRunRecord(
        result=result,
        artifacts=ResearchRunArtifacts(
            json_path=json_path,
            json_sha256_path=json_sha_path,
            markdown_path=markdown_path,
            markdown_sha256_path=markdown_sha_path,
            json_sha256=json_sha,
            markdown_sha256=markdown_sha,
        ),
    )


def _verify_sidecar(path: Path, sidecar: Path) -> str:
    if not path.is_file() or not sidecar.is_file():
        raise FileNotFoundError(f"research artifact is incomplete: {path.name}")
    expected = sidecar.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError(f"invalid checksum sidecar for {path.name}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"research artifact checksum mismatch: {path.name}")
    return actual


def _validate_session_id(session_id: str) -> None:
    if not _SESSION_ID.fullmatch(session_id) or session_id in {".", ".."}:
        raise ValueError("session_id must be a safe stable artifact identifier")


__all__ = [
    "ResearchRunArtifacts",
    "ResearchRunRecord",
    "execute_research_ask",
    "load_research_run",
]
