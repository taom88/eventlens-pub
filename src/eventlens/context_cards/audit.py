"""Independent, fail-closed release audit for Context Card v4.

The audit is an observation layer.  It hashes and inspects the supplied card,
records findings, and decides whether a human approval transition is even
permitted.  It never repairs the card or performs that transition.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .models import ContextCardV4
from .qc import QCIssue, QCReport, QCSeverity, detect_staleness, validate_card

if TYPE_CHECKING:
    from eventlens.agents.context_card_audit import ContextCardAuditor


AUDIT_SCHEMA_VERSION = "eventlens.context_card.audit.v1"
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class AuditSeverity(StrEnum):
    WARNING = "warning"
    MATERIAL = "material"
    CRITICAL = "critical"


class AuditCategory(StrEnum):
    TEMPORAL_LEAKAGE = "temporal_leakage"
    GAAP_MISMATCH = "gaap_mismatch"
    PERIOD_BASIS_MISMATCH = "period_basis_mismatch"
    DANGLING_EVIDENCE = "dangling_evidence"
    SPEAKER_ERROR = "speaker_error"
    CAUSAL_OVERSTATEMENT = "causal_overstatement"
    STALE_DATA = "stale_data"
    FALSE_PRECISION = "false_precision"
    NULL_ZERO_CONFUSION = "null_zero_confusion"
    HORIZON_AMBIGUITY = "horizon_ambiguity"
    PROGRAMMATIC_QC = "programmatic_qc"
    RELEASE_GOVERNANCE = "release_governance"


class ReleaseDisposition(StrEnum):
    BLOCKED = "blocked"
    AWAITING_HUMAN_SIGNOFF = "awaiting_human_signoff"
    ELIGIBLE_FOR_HUMAN_APPROVAL = "eligible_for_human_approval"


class _AuditModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True
    )


class AuditFinding(_AuditModel):
    """One independently actionable defect; none of these fields is prose-only."""

    finding_id: NonEmptyStr
    category: AuditCategory
    field_path: NonEmptyStr
    failure_mode: NonEmptyStr
    downstream_inference: NonEmptyStr
    severity: AuditSeverity
    deterministic_prevention: NonEmptyStr

    @property
    def blocking(self) -> bool:
        return self.severity in {AuditSeverity.MATERIAL, AuditSeverity.CRITICAL}


class HumanReleaseSignoffs(_AuditModel):
    """Explicit human gates.  Defaults never imply consent."""

    independent_audit_reviewed: bool = False
    unresolved_warnings_accepted: bool = False
    source_rights_confirmed: bool = False
    action_policy_approved: bool = False
    lifecycle_migration_approved: bool = False

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(
            name for name, value in self.model_dump().items() if value is not True
        )


class AdversarialAuditReport(_AuditModel):
    schema_version: Literal[AUDIT_SCHEMA_VERSION] = AUDIT_SCHEMA_VERSION
    auditor_id: NonEmptyStr
    independent_auditor: Literal[True] = True
    audited_at: AwareDatetime
    audited_card_sha256: Sha256
    artifact_hashes: dict[NonEmptyStr, Sha256]
    generation_manifest: dict[str, Any]
    generation_manifest_sha256: Sha256
    audit_manifest_sha256: Sha256
    qc_report: QCReport
    findings: tuple[AuditFinding, ...] = ()
    human_signoffs: HumanReleaseSignoffs
    pending_human_signoffs: tuple[NonEmptyStr, ...]
    release_disposition: ReleaseDisposition
    approval_permitted: bool

    @property
    def material_findings(self) -> tuple[AuditFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @model_validator(mode="after")
    def release_gate_is_fail_closed(self) -> "AdversarialAuditReport":
        expected_pending = self.human_signoffs.pending
        if self.pending_human_signoffs != expected_pending:
            raise ValueError("pending_human_signoffs must match explicit signoff state")
        has_blocker = bool(self.qc_report.blockers or self.material_findings)
        expected_disposition = (
            ReleaseDisposition.BLOCKED
            if has_blocker
            else ReleaseDisposition.AWAITING_HUMAN_SIGNOFF
            if self.pending_human_signoffs
            else ReleaseDisposition.ELIGIBLE_FOR_HUMAN_APPROVAL
        )
        if self.release_disposition is not expected_disposition:
            if has_blocker:
                raise ValueError("QC or material audit findings must block release")
            raise ValueError("release disposition must match human signoff state")
        expected_permission = (
            expected_disposition is ReleaseDisposition.ELIGIBLE_FOR_HUMAN_APPROVAL
        )
        if self.approval_permitted is not expected_permission:
            raise ValueError(
                "approval permission must match the fail-closed release disposition"
            )
        return self


def audit_context_card(
    card: ContextCardV4 | Mapping[str, Any],
    auditor: "ContextCardAuditor | None" = None,
    *,
    audited_at: datetime | None = None,
    human_signoffs: HumanReleaseSignoffs | Mapping[str, bool] | None = None,
    artifact_hashes: Mapping[str, str] | None = None,
    analytical_uses: Mapping[str, Iterable[str]] | None = None,
    latest_completed_fiscal_period: str | None = None,
    current_event_datetime: datetime | None = None,
    latest_consensus_timestamp: datetime | None = None,
    consensus_max_age: timedelta = timedelta(days=14),
) -> AdversarialAuditReport:
    """Run both deterministic QC and independent adversarial interpretation.

    Freshness observations are explicit inputs.  The wall clock is used only
    as the audit timestamp; it is never silently treated as market evidence.
    """

    document = _json_document(card)
    moment = _aware(audited_at or datetime.now(timezone.utc), "audited_at")
    freshness: tuple[QCIssue, ...] = ()
    try:
        parsed = ContextCardV4.model_validate(document)
    except Exception:
        parsed = None
    if parsed is not None and any(
        value is not None
        for value in (
            latest_completed_fiscal_period,
            current_event_datetime,
            latest_consensus_timestamp,
        )
    ):
        freshness = detect_staleness(
            parsed,
            now=moment,
            latest_completed_fiscal_period=latest_completed_fiscal_period,
            current_event_datetime=current_event_datetime,
            latest_consensus_timestamp=latest_consensus_timestamp,
            consensus_max_age=consensus_max_age,
        )
    qc_report = validate_card(
        document,
        analytical_uses=analytical_uses,
        freshness_issues=freshness,
    )

    if auditor is None:
        from eventlens.agents.context_card_audit import DeterministicContextCardAuditor

        auditor = DeterministicContextCardAuditor()
    findings = list(auditor.audit(document, qc_report=qc_report))
    findings.extend(_uncovered_qc_findings(qc_report, findings))
    findings = _unique_findings(findings)

    signoffs = (
        human_signoffs
        if isinstance(human_signoffs, HumanReleaseSignoffs)
        else HumanReleaseSignoffs.model_validate(human_signoffs or {})
    )
    card_hash = _sha256(document)
    provenance = document.get("provenance")
    generation_manifest = dict(provenance) if isinstance(provenance, Mapping) else {}
    generation_hash = _sha256(generation_manifest)
    hashes = {**dict(artifact_hashes or {}), "context_card_json": card_hash}
    manifest_hash = _sha256(
        {
            "audited_card_sha256": card_hash,
            "artifact_hashes": hashes,
            "generation_manifest_sha256": generation_hash,
            "qc_report": qc_report.model_dump(mode="json"),
            "findings": [item.model_dump(mode="json") for item in findings],
            "human_signoffs": signoffs.model_dump(mode="json"),
        }
    )
    blocked = bool(qc_report.blockers or any(item.blocking for item in findings))
    if blocked:
        disposition = ReleaseDisposition.BLOCKED
    elif signoffs.pending:
        disposition = ReleaseDisposition.AWAITING_HUMAN_SIGNOFF
    else:
        disposition = ReleaseDisposition.ELIGIBLE_FOR_HUMAN_APPROVAL
    return AdversarialAuditReport(
        auditor_id=auditor.auditor_id,
        audited_at=moment,
        audited_card_sha256=card_hash,
        artifact_hashes=hashes,
        generation_manifest=generation_manifest,
        generation_manifest_sha256=generation_hash,
        audit_manifest_sha256=manifest_hash,
        qc_report=qc_report,
        findings=tuple(findings),
        human_signoffs=signoffs,
        pending_human_signoffs=signoffs.pending,
        release_disposition=disposition,
        approval_permitted=disposition is ReleaseDisposition.ELIGIBLE_FOR_HUMAN_APPROVAL,
    )


def write_audit_report(report: AdversarialAuditReport, path: Path) -> str:
    """Persist one structured report atomically and return its payload hash."""

    payload = json.dumps(
        report.model_dump(mode="json"), sort_keys=True, indent=2, separators=(",", ": ")
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _uncovered_qc_findings(
    report: QCReport, existing: list[AuditFinding]
) -> list[AuditFinding]:
    covered = {(item.field_path, item.severity.value) for item in existing}
    findings: list[AuditFinding] = []
    for issue in report.issues:
        severity = (
            AuditSeverity.MATERIAL
            if issue.severity is QCSeverity.BLOCKER
            or issue.code.value
            in {"new_quarter_available", "event_date_changed", "consensus_aged"}
            else AuditSeverity.WARNING
        )
        path = issue.field_path or "$"
        if (path, severity.value) in covered:
            continue
        findings.append(
            AuditFinding(
                finding_id=f"qc-{issue.code.value}-{len(findings) + 1}",
                category=_qc_category(issue),
                field_path=path,
                failure_mode=issue.message,
                downstream_inference="A downstream consumer could treat a QC-invalid field as release-quality evidence.",
                severity=severity,
                deterministic_prevention=f"Require QC code {issue.code.value} to clear before the approval transition.",
            )
        )
    return findings


def _qc_category(issue: QCIssue) -> AuditCategory:
    code = issue.code.value
    if "cutoff" in code:
        return AuditCategory.TEMPORAL_LEAKAGE
    if "basis" in code or "comparability" in code:
        return AuditCategory.GAAP_MISMATCH
    if "evidence" in code or "receipt" in code or "operand" in code:
        return AuditCategory.DANGLING_EVIDENCE
    if "speaker" in code:
        return AuditCategory.SPEAKER_ERROR
    if "attribution" in code:
        return AuditCategory.CAUSAL_OVERSTATEMENT
    if code in {"new_quarter_available", "event_date_changed", "consensus_aged"}:
        return AuditCategory.STALE_DATA
    if "horizon" in code:
        return AuditCategory.HORIZON_AMBIGUITY
    if "threshold" in code:
        return AuditCategory.FALSE_PRECISION
    return AuditCategory.PROGRAMMATIC_QC


def _unique_findings(findings: Iterable[AuditFinding]) -> list[AuditFinding]:
    result: list[AuditFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        key = (finding.category.value, finding.field_path, finding.failure_mode)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result


def _json_document(card: ContextCardV4 | Mapping[str, Any]) -> dict[str, Any]:
    raw = card.model_dump(mode="json") if isinstance(card, ContextCardV4) else dict(card)
    return json.loads(json.dumps(raw, sort_keys=True, default=str))


def _sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)
