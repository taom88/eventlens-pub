from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from eventlens.context_cards.audit import (
    AdversarialAuditReport,
    AuditCategory,
    AuditFinding,
    AuditSeverity,
    HumanReleaseSignoffs,
    ReleaseDisposition,
    audit_context_card,
    write_audit_report,
)

from .test_qc import _minimal_v4_card


AUDITED_AT = datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_audit_is_read_only_and_persists_exact_structured_report(tmp_path) -> None:
    document = _minimal_v4_card()
    before = copy.deepcopy(document)
    report = audit_context_card(document, audited_at=AUDITED_AT)
    destination = tmp_path / "audit.json"

    payload_hash = write_audit_report(report, destination)
    persisted = AdversarialAuditReport.model_validate_json(destination.read_text())

    assert document == before
    assert persisted == report
    assert len(payload_hash) == 64
    assert persisted.artifact_hashes["context_card_json"] == persisted.audited_card_sha256
    assert persisted.generation_manifest == document["provenance"]
    assert persisted.release_disposition is ReleaseDisposition.AWAITING_HUMAN_SIGNOFF
    assert not persisted.approval_permitted


def test_explicit_human_signoffs_are_required_for_approval() -> None:
    signoffs = HumanReleaseSignoffs(
        independent_audit_reviewed=True,
        unresolved_warnings_accepted=True,
        source_rights_confirmed=True,
        action_policy_approved=True,
        lifecycle_migration_approved=True,
    )

    report = audit_context_card(
        _minimal_v4_card(), audited_at=AUDITED_AT, human_signoffs=signoffs
    )

    assert report.release_disposition is ReleaseDisposition.ELIGIBLE_FOR_HUMAN_APPROVAL
    assert report.approval_permitted


def test_report_model_rejects_approval_despite_a_material_defect() -> None:
    report = audit_context_card(_minimal_v4_card(), audited_at=AUDITED_AT)
    finding = AuditFinding(
        finding_id="material",
        category=AuditCategory.STALE_DATA,
        field_path="identity.card.status",
        failure_mode="stale",
        downstream_inference="stale input could drive a decision",
        severity=AuditSeverity.MATERIAL,
        deterministic_prevention="regenerate",
    )
    payload = report.model_dump(mode="json")
    payload["findings"] = [finding.model_dump(mode="json")]
    payload["release_disposition"] = "eligible_for_human_approval"
    payload["approval_permitted"] = True
    payload["human_signoffs"] = {
        key: True for key in HumanReleaseSignoffs.model_fields
    }
    payload["pending_human_signoffs"] = []

    with pytest.raises(ValueError, match="must block release"):
        AdversarialAuditReport.model_validate(payload)
