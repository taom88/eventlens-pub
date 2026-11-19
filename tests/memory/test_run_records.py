from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from eventlens.memory import (
    DurableRunRecord,
    ImmutableRevisionError,
    RunArtifactReference,
    RunRecordKind,
    SQLiteResearchMemoryRepository,
    SupabaseResearchMemoryRepository,
    canonical_run_record_checksum,
)


def _record(kind: RunRecordKind = RunRecordKind.TRANSITION, revision: int = 1) -> DurableRunRecord:
    values = dict(
        id=uuid4(), owner_id="owner-1", workspace_id="workspace-1", run_id=RUN_ID,
        session_id=SESSION_ID, kind=kind, revision=revision,
        created_at=datetime(2026, 10, 30, tzinfo=timezone.utc), status="awaiting_external_agent",
        metadata={"from": "evidence_frozen", "to": "awaiting_external_agent"},
        artifact=(RunArtifactReference(artifact_id="bundle", storage_key="sha256/ab/bundle.json",
                                      sha256="a" * 64, media_type="application/json", size_bytes=42)
                  if kind is RunRecordKind.ARTIFACT else None),
    )
    draft = DurableRunRecord.model_construct(**values, checksum="0" * 64)
    return DurableRunRecord.model_validate({
        **draft.model_dump(mode="json", exclude={"checksum"}),
        "checksum": canonical_run_record_checksum(draft),
    })


RUN_ID, SESSION_ID = uuid4(), uuid4()


@pytest.mark.parametrize("kind", list(RunRecordKind))
def test_sqlite_reopen_preserves_immutable_scoped_run_records(tmp_path: Path, kind: RunRecordKind) -> None:
    path = tmp_path / "memory.sqlite"
    first = SQLiteResearchMemoryRepository(path)
    record = _record(kind)
    first.save_run_record(record)
    reopened = SQLiteResearchMemoryRepository(path)
    assert reopened.get_run_record("workspace-1", RUN_ID, kind, 1) == record
    assert reopened.get_run_record("wrong-workspace", RUN_ID, kind, 1) is None
    assert reopened.list_run_records("workspace-1", RUN_ID) == (record,)
    with pytest.raises(ImmutableRevisionError):
        reopened.save_run_record(record.model_copy(update={"status": "changed"}))


def test_run_record_requires_artifact_reference_for_artifact_kind() -> None:
    with pytest.raises(ValueError, match="artifact records require"):
        base = _record()
        forged = base.model_dump(mode="json", exclude={"checksum"})
        forged.update(kind=RunRecordKind.ARTIFACT.value, artifact=None)
        forged["checksum"] = canonical_run_record_checksum(forged)
        DurableRunRecord.model_validate(forged)


def test_supabase_fresh_repository_client_rehydrates_records() -> None:
    rows: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            row = request.read()
            import json
            rows.append(json.loads(row))
            return httpx.Response(201, json=[])
        params = dict(request.url.params)
        matches = [row for row in rows
                   if row["workspace_id"] == params.get("workspace_id", "")[3:]
                   and row["run_id"] == params.get("run_id", "")[3:]
                   and ("kind" not in params or row["kind"] == params["kind"][3:])
                   and ("revision" not in params or row["revision"] == int(params["revision"][3:]))]
        return httpx.Response(200, json=matches)

    record = _record(RunRecordKind.EVIDENCE_BUNDLE)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    SupabaseResearchMemoryRepository(url="https://project.test", secret_key="server", client=client).save_run_record(record)
    # A new adapter and HTTP client reads the persisted server-side row.
    reopened = SupabaseResearchMemoryRepository(
        url="https://project.test", secret_key="server",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert reopened.get_run_record("workspace-1", RUN_ID, record.kind, 1) == record
    assert reopened.list_run_records("other", RUN_ID) == ()


def test_run_record_checksum_rejects_forged_semantic_content() -> None:
    record = _record()
    forged = record.model_dump(mode="json")
    forged["metadata"]["to"] = "locked"
    with pytest.raises(ValueError, match="checksum does not match semantic content"):
        DurableRunRecord.model_validate(forged)
