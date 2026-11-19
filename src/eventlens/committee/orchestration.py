"""Deterministic orchestration guards for independent EventLens desks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping

from .models import (
    CommitteeRole,
    DeskMandate,
    DeskReport,
    Disagreement,
    DisagreementRegister,
    EvidenceSnapshot,
    RiskAssessment,
)

DeskRunner = Callable[[EvidenceSnapshot, DeskMandate], DeskReport]

INDEPENDENT_DESK_ROLES = frozenset(
    {
        CommitteeRole.EARNINGS_RESEARCH,
        CommitteeRole.GLOBAL_EQUITIES,
        CommitteeRole.QUANT_STRATEGY,
        CommitteeRole.TECHNICAL_PANEL,
        CommitteeRole.RISK_COMMITTEE,
    }
)


def run_independent_desks(
    snapshot: EvidenceSnapshot,
    mandates: tuple[DeskMandate, ...],
    runners: Mapping[CommitteeRole, DeskRunner],
) -> tuple[DeskReport, ...]:
    """Run first-pass desks without exposing any other desk output.

    The API intentionally gives every runner only ``snapshot`` and its own
    mandate; disagreement/adjudication happens in a later explicit phase.
    """

    roles = tuple(mandate.role for mandate in mandates)
    if len(roles) != len(set(roles)):
        raise ValueError("each desk role may have exactly one first-pass mandate")
    if set(roles) != INDEPENDENT_DESK_ROLES:
        raise ValueError("first-pass mandates must cover every independent desk role")
    if set(runners) != INDEPENDENT_DESK_ROLES:
        raise ValueError("first-pass runners must cover every independent desk role")
    reports: list[DeskReport] = []
    for mandate in mandates:
        report = runners[mandate.role](snapshot, mandate)
        if report.role is not mandate.role or report.snapshot_id != snapshot.snapshot_id:
            raise ValueError("desk report must match its mandate role and immutable snapshot")
        validate_report_against_snapshot(report, snapshot)
        reports.append(report)
    return tuple(reports)


def validate_report_against_snapshot(report: DeskReport, snapshot: EvidenceSnapshot) -> None:
    """Reject citations, metrics, and declared gaps not present in the ledger."""

    if report.snapshot_id != snapshot.snapshot_id:
        raise ValueError("desk report must reference the immutable evidence snapshot")
    evidence_ids = {(item.source_id, item.passage_id) for item in snapshot.evidence}
    metric_ids = {item.metric_id for item in snapshot.canonical_metrics}
    gap_ids = {item.gap_id for item in snapshot.evidence_gaps}
    for claim in report.claims:
        if any(
            (item.source_id, item.passage_id) not in evidence_ids
            for item in claim.evidence_references
        ) or any(item not in metric_ids for item in claim.metric_ids):
            raise ValueError("desk claim references evidence or metrics outside the snapshot")
        if any(item not in gap_ids for item in claim.missing_data_gap_ids):
            raise ValueError("desk claim references an evidence gap outside the snapshot")


def build_disagreement_register(reports: tuple[DeskReport, ...]) -> DisagreementRegister:
    """Create a deterministic register without voting away minority reports."""

    reports = tuple(
        report
        for report in reports
        if report.role
        not in {
            CommitteeRole.ADJUDICATOR,
            CommitteeRole.INVESTMENT_COMMITTEE,
        }
    )
    if not reports:
        raise ValueError("at least one persisted desk report is required")
    claim_groups: dict[str, list[tuple[DeskReport, object]]] = {}
    unsupported: list[str] = []
    for report in reports:
        for claim in report.claims:
            if claim.support_status.value == "draft_unsupported":
                unsupported.append(claim.claim_id)
                continue
            claim_groups.setdefault(claim.proposition_id, []).append((report, claim))

    consensus: list[str] = []
    disputed: list[Disagreement] = []
    timing_sensitive: set[str] = set()
    missing_gap_ids: set[str] = set()
    minority_reports: set[str] = set()
    for proposition_id in sorted(claim_groups):
        entries = claim_groups[proposition_id]
        statements = {entry[1].statement.strip().casefold() for entry in entries}  # type: ignore[attr-defined]
        stances = {entry[1].stance for entry in entries}  # type: ignore[attr-defined]
        if (
            len(entries) > 1
            and (len(statements) == 1 or (len(stances) == 1 and "unspecified" not in stances))
        ):
            consensus.append(proposition_id)
        elif len(entries) > 1:
            representative = entries[0][1]
            assumptions = tuple(
                sorted({item for _, claim in entries for item in claim.assumption_ids})  # type: ignore[attr-defined]
            )
            report_ids = tuple(sorted(report.report_id for report, _ in entries))
            minority_reports.update(report_ids)
            if any(claim.timing_sensitive for _, claim in entries):  # type: ignore[attr-defined]
                timing_sensitive.add(proposition_id)
            missing_gap_ids.update(
                gap_id for _, claim in entries for gap_id in claim.missing_data_gap_ids  # type: ignore[attr-defined]
            )
            disputed.append(
                Disagreement(
                    claim_id=representative.claim_id,  # type: ignore[attr-defined]
                    proposition_id=proposition_id,
                    topic_id=representative.topic_id,  # type: ignore[attr-defined]
                    participating_reports=report_ids,
                    category="conflicting_claim",
                    detail="Independent desks made materially different claims for the same claim ID.",
                    differing_assumptions=assumptions,
                    resolving_evidence=("additional passage-addressable evidence or refreshed canonical metric",),
                )
            )
    values = {
        "snapshot_id": reports[0].snapshot_id,
        "reports": [
            report.model_dump(mode="json")
            for report in sorted(reports, key=lambda item: item.report_id)
        ],
        "consensus": sorted(consensus),
        "disputed": [item.model_dump(mode="json") for item in disputed],
        "unsupported": sorted(set(unsupported)),
        "minority": sorted(minority_reports),
        "timing": sorted(timing_sensitive),
        "gaps": sorted(missing_gap_ids),
    }
    adjudication_id = "adjudication-" + hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DisagreementRegister(
        adjudication_id=adjudication_id,
        consensus_claim_ids=tuple(consensus),
        consensus_proposition_ids=tuple(consensus),
        disputed=tuple(disputed),
        unsupported_claim_ids=tuple(sorted(set(unsupported))),
        minority_report_ids=tuple(sorted(minority_reports)),
        timing_sensitive_proposition_ids=tuple(sorted(timing_sensitive)),
        missing_data_gap_ids=tuple(sorted(missing_gap_ids)),
        unresolved_questions=tuple(
            "Resolve each disputed proposition with retained evidence or a refreshed canonical metric."
            for _ in disputed
        ),
    )


def risk_vetoes_decision(risk: RiskAssessment) -> bool:
    """A tiny explicit gate used by a later packet builder or workflow."""

    return risk.escalation.value == "veto"
