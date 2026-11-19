from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eventlens.persistence import AuditEvent, AuditIdempotencyConflictError, JSONLAuditStore


def make_event(**changes: object) -> AuditEvent:
    values: dict[str, object] = {
        "event_id": "audit-event-1",
        "event_type": "vibe_trading.run_started",
        "session_id": "session-1",
        "event_scope_id": "demo-2026-01-28",
        "audit_correlation_id": "correlation-1",
        "idempotency_key": "run-start-1",
        "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "payload": {"transport": "mock"},
    }
    values.update(changes)
    return AuditEvent(**values)


def test_append_is_idempotent_and_preserves_a_single_jsonl_record(tmp_path: Path) -> None:
    store = JSONLAuditStore(tmp_path / "audit_log.jsonl")
    event = make_event()

    assert store.append(event).appended is True
    assert store.append(event.model_copy(update={"event_id": "audit-event-2"})).appended is False
    assert store.read_all() == (event,)


def test_idempotency_key_cannot_be_reused_for_another_logical_event(tmp_path: Path) -> None:
    store = JSONLAuditStore(tmp_path / "audit_log.jsonl")
    store.append(make_event())

    with pytest.raises(AuditIdempotencyConflictError):
        store.append(make_event(payload={"transport": "fixture"}))


def test_concurrent_equivalent_appends_do_not_duplicate_records(tmp_path: Path) -> None:
    store = JSONLAuditStore(tmp_path / "audit_log.jsonl")
    event = make_event()
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _: store.append(event), range(8)))

    assert sum(outcome.appended for outcome in outcomes) == 1
    assert len(store.read_all()) == 1


def test_new_event_uses_injected_clock_and_identifier(tmp_path: Path) -> None:
    instant = datetime(2026, 2, 1, tzinfo=timezone.utc)
    store = JSONLAuditStore(tmp_path / "audit_log.jsonl", clock=lambda: instant, id_factory=lambda: "generated-id")

    event = store.new_event(
        event_type="vibe_trading.run_warning",
        session_id="session-1",
        event_scope_id="event-1",
        audit_correlation_id="correlation-1",
        idempotency_key="warning-1",
    )

    assert event.event_id == "generated-id"
    assert event.occurred_at == instant


def test_audit_redacts_prompts_and_credentials(tmp_path: Path) -> None:
    store = JSONLAuditStore(tmp_path / "audit_log.jsonl")
    event = make_event(payload={"question": "unredacted prompt", "token": "not-for-disk", "status": "started"})

    outcome = store.append(event)

    assert outcome.event.payload == {"question": "[REDACTED]", "token": "[REDACTED]", "status": "started"}
    assert "unredacted prompt" not in store.path.read_text(encoding="utf-8")


def test_audit_redacts_common_nested_secret_key_variants(tmp_path: Path) -> None:
    event = make_event(
        payload={
            "nested": {
                "access_token": "token-value",
                "client-secret": "secret-value",
                "db_password": "password-value",
            }
        }
    )

    assert event.payload == {
        "nested": {
            "access_token": "[REDACTED]",
            "client-secret": "[REDACTED]",
            "db_password": "[REDACTED]",
        }
    }
