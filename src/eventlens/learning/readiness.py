"""Learning-readiness audit over persisted, immutable artifacts."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .models import ReadinessAudit


MINIMUM_COMPLETE_EVENTS = 50


def audit_learning_readiness(
    workflow_artifact_dir: Path,
    *,
    outcome_dir: Path | None = None,
    postmortem_dir: Path | None = None,
    minimum_complete_events: int = MINIMUM_COMPLETE_EVENTS,
    clock=lambda: datetime.now(timezone.utc),
) -> ReadinessAudit:
    packet_paths = tuple(workflow_artifact_dir.glob("**/decision-packet.json"))
    packets = []
    missing_fields: Counter[str] = Counter()
    missing_providers: Counter[str] = Counter()
    schema_versions: set[str] = set()
    unique_events: set[tuple[str, str]] = set()
    for path in packet_paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing_fields["unreadable_decision_packet"] += 1
            continue
        packet = document.get("decision_packet", document)
        packets.append(packet)
        schema_versions.add(str(document.get("schema_version", packet.get("schema_version", "missing"))))
        ticker = str(packet.get("ticker", document.get("ticker", "")))
        event_id = str(packet.get("event_id", document.get("event_id", "")))
        if ticker and event_id:
            unique_events.add((ticker, event_id))
        for field in (
            "snapshot",
            "desk_reports",
            "risk_assessment",
            "disagreement_register",
            "canonical_expected_move_status",
        ):
            if packet.get(field) in (None, [], {}):
                missing_fields[field] += 1
        for record in packet.get("provenance_manifest", []):
            if not record.get("reference"):
                missing_providers[record.get("provenance_id", "unknown")] += 1

    outcome_paths = tuple((outcome_dir or workflow_artifact_dir).glob("**/outcome-record.json"))
    postmortem_paths = tuple(
        (postmortem_dir or workflow_artifact_dir).glob("**/approved-postmortem.json")
    )
    complete = min(len(outcome_paths), len(postmortem_paths), len(unique_events))
    schema_drift = len(schema_versions) > 1 or "missing" in schema_versions
    ready = complete >= minimum_complete_events and not schema_drift
    reasons = []
    if complete < minimum_complete_events:
        reasons.append(
            f"{complete} complete unique events; minimum is {minimum_complete_events}"
        )
    if not outcome_paths:
        reasons.append("no immutable post-event outcome records were found")
    if not postmortem_paths:
        reasons.append("no approved postmortems were found")
    if len(unique_events) < len(packet_paths):
        reasons.append("persisted packets include duplicate replay sessions")
    if schema_drift:
        reasons.append("schema drift requires migration before training")
    return ReadinessAudit(
        audited_at=clock(),
        persisted_packet_count=len(packet_paths),
        unique_event_count=len(unique_events),
        complete_outcome_count=len(outcome_paths),
        approved_postmortem_count=len(postmortem_paths),
        schema_versions=tuple(sorted(schema_versions)),
        schema_drift_detected=schema_drift,
        missingness_by_field=dict(sorted(missing_fields.items())),
        missingness_by_provider=dict(sorted(missing_providers.items())),
        minimum_complete_events=minimum_complete_events,
        ready_for_training=ready,
        permitted_mode="offline_training" if ready else "evaluation_and_shadow_only",
        reasons=tuple(reasons),
    )
