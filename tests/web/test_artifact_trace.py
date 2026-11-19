import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eventlens.web.artifact_trace import (
    ArtifactTraceReader,
    ArtifactTraceSpec,
    AuditTraceReader,
    TraceStatus,
    read_audit_trace,
)


def test_missing_artifact_is_explicit_and_keeps_expected_metadata(tmp_path: Path) -> None:
    path = tmp_path / "decision-packet.json"
    record = ArtifactTraceReader(tmp_path).read(
        ArtifactTraceSpec(
            path=path,
            artifact_id="packet-1",
            artifact_type="decision_packet",
            schema_version="1.0",
            expected_sha256="a" * 64,
        )
    )

    assert record.status is TraceStatus.MISSING
    assert record.artifact_id == "packet-1"
    assert record.schema_version == "1.0"
    assert record.expected_checksum == "a" * 64
    assert record.content is None
    assert "missing" in (record.warning or "")


def test_checksum_mismatch_is_corrupt_and_never_renders_content(tmp_path: Path) -> None:
    path = tmp_path / "packet.json"
    path.write_text(
        json.dumps({"schema_version": "1.0", "secret": "must-not-render", "ok": "value"}),
        encoding="utf-8",
    )
    record = ArtifactTraceReader(tmp_path).read(
        ArtifactTraceSpec(path=path, expected_sha256="b" * 64)
    )

    assert record.status is TraceStatus.CORRUPT
    assert record.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record.content is None
    assert record.warning == "artifact file checksum mismatch"


def test_json_markdown_and_audit_rendering_redacts_private_values(tmp_path: Path) -> None:
    json_path = tmp_path / "report.json"
    json_path.write_text(
        json.dumps(
            {
                "schema_version": "report.v1",
                "artifact_type": "research_report",
                "api_key": "json-secret",
                "question": "private earnings question",
                "nested": {"Authorization": "Bearer json-token", "safe": "visible"},
            }
        ),
        encoding="utf-8",
    )
    markdown_path = tmp_path / "report.md"
    markdown_path.write_text(
        "# Report\n\nQuestion: private earnings question\n\nToken: markdown-secret\n",
        encoding="utf-8",
    )
    json_record = ArtifactTraceReader(tmp_path).read(json_path)
    markdown_record = ArtifactTraceReader(tmp_path).read(markdown_path)

    assert json_record.status is TraceStatus.AVAILABLE
    rendered_json = json.dumps(json_record.content)
    assert "json-secret" not in rendered_json
    assert "private earnings question" not in rendered_json
    assert "json-token" not in rendered_json
    assert json_record.content["nested"]["safe"] == "visible"
    assert "private earnings question" not in (markdown_record.content or "")
    assert "markdown-secret" not in (markdown_record.content or "")

    audit_path = tmp_path / "audit.jsonl"
    lines = [
        {
            "schema_version": "1.0",
            "event_id": "b",
            "event_type": "run_finished",
            "session_id": "s",
            "event_scope_id": "e",
            "audit_correlation_id": "c",
            "idempotency_key": "k-b",
            "occurred_at": "2026-01-01T00:00:02Z",
            "payload": {"prompt": "private prompt", "api_key": "audit-secret"},
        }
    ]
    audit_path.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")
    audit = read_audit_trace(audit_path)
    assert audit.status is TraceStatus.AVAILABLE
    payload = audit.events[0].payload
    assert payload == {"prompt": "[REDACTED]", "api_key": "[REDACTED]"}
    assert "private prompt" not in audit.model_dump_json()
    assert "audit-secret" not in audit.model_dump_json()


def test_json_redaction_removes_bearer_tails_and_local_paths(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    bearer_tail = "eyJhbGciOiJIUzI1NiJ9.secret-tail"
    payload = {
        "schema_version": "report.v1",
        "artifact_type": "research_report",
        "status": "validated",
        "authorization": f"Bearer {bearer_tail}",
        "bundle_path": str(tmp_path / "bundle-r1.json"),
        "report_path": str(path),
        "details": f"stored at {path}",
        "safe": "visible metadata",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    record = ArtifactTraceReader(tmp_path).read(path)

    rendered = record.model_dump_json()
    assert record.status is TraceStatus.AVAILABLE
    assert record.schema_version == "report.v1"
    assert record.artifact_type == "research_report"
    assert record.checksum is not None
    assert record.content["schema_version"] == "report.v1"
    assert record.content["status"] == "validated"
    assert record.content["safe"] == "visible metadata"
    assert bearer_tail not in rendered
    assert str(tmp_path) not in rendered
    assert record.content["bundle_path"] == "[REDACTED]"
    assert record.content["report_path"] == "[REDACTED]"


def test_audit_events_have_stable_time_then_id_order(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    event_template = {
        "schema_version": "1.0",
        "event_type": "run_warning",
        "session_id": "s",
        "event_scope_id": "e",
        "audit_correlation_id": "c",
        "payload": {},
    }
    rows = [
        {
            **event_template,
            "event_id": "z",
            "idempotency_key": "z-key",
            "occurred_at": "2026-01-01T00:00:02+00:00",
        },
        {
            **event_template,
            "event_id": "b",
            "idempotency_key": "b-key",
            "occurred_at": "2026-01-01T00:00:01+00:00",
        },
        {
            **event_template,
            "event_id": "a",
            "idempotency_key": "a-key",
            "occurred_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = AuditTraceReader(path).read()
    assert [event.event_id for event in result.events] == ["a", "b", "z"]
    assert result.events[0].occurred_at.tzinfo is not None


def test_invalid_audit_line_is_explicit_without_echoing_untrusted_input(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text('{"prompt":"do-not-echo", "event_id":\n', encoding="utf-8")

    result = AuditTraceReader(path).read()
    assert result.status is TraceStatus.CORRUPT
    assert result.events == ()
    assert result.warnings == ("invalid audit event at line 1",)
    assert "do-not-echo" not in result.model_dump_json()


def test_trace_paths_are_relative_and_symlinks_are_rejected(tmp_path: Path) -> None:
    nested = tmp_path / "session" / "event"
    nested.mkdir(parents=True)
    target = nested / "packet.json"
    target.write_text('{"schema_version":"1.0","safe":true}', encoding="utf-8")
    record = ArtifactTraceReader(tmp_path).read(target)
    assert record.status is TraceStatus.AVAILABLE
    assert record.path == "session/event/packet.json"
    assert str(tmp_path) not in record.model_dump_json()

    outside = tmp_path.parent / "outside-packet.json"
    outside.write_text('{"safe":false}', encoding="utf-8")
    try:
        (nested / "escape.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    escaped = ArtifactTraceReader(tmp_path).read(nested / "escape.json")
    assert escaped.status is TraceStatus.CORRUPT
    assert escaped.content is None
    assert "symlink" in (escaped.warning or "")
    assert "safe" not in escaped.model_dump_json()


def test_reader_list_is_non_recursive_and_read_many_order_is_stable(tmp_path: Path) -> None:
    (tmp_path / "z.json").write_text('{"z":1}', encoding="utf-8")
    (tmp_path / "a.md").write_text("# A", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "hidden.json").write_text('{"hidden":true}', encoding="utf-8")
    records = ArtifactTraceReader(tmp_path).list()
    assert [record.path for record in records] == ["a.md", "z.json"]


def test_adjacent_checksum_sidecar_is_verified_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "packet.json"
    path.write_text('{"safe":true}', encoding="utf-8")
    sidecar = path.with_name(path.name + ".sha256")
    sidecar.write_text("0" * 64 + "\n", encoding="ascii")
    before = sidecar.read_text(encoding="ascii")
    record = ArtifactTraceReader(tmp_path).read(path)
    assert record.status is TraceStatus.CORRUPT
    assert record.expected_checksum == "0" * 64
    assert sidecar.read_text(encoding="ascii") == before


def test_audit_reader_filters_other_sessions_without_exposing_full_path(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    template = {
        "schema_version": "1.0",
        "event_type": "run_finished",
        "event_scope_id": "event-1",
        "audit_correlation_id": "corr",
        "payload": {},
    }
    rows = [
        {**template, "event_id": "ok", "session_id": "session-1", "idempotency_key": "ok", "occurred_at": "2026-01-01T00:00:00Z"},
        {**template, "event_id": "other", "session_id": "session-2", "idempotency_key": "other", "occurred_at": "2026-01-01T00:00:01Z"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = AuditTraceReader(path, root=tmp_path, session_id="session-1", event_scope_id="event-1").read()
    assert [event.event_id for event in result.events] == ["ok"]
    assert result.path == "audit.jsonl"
    assert str(tmp_path) not in result.model_dump_json()
