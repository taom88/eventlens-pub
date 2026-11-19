"""One release authority for the complete PRD outcome.

Component tests can prove local invariants, but only this audit represents the
user-visible MVP claim.  It intentionally remains blocked until the canonical
packet, independent QA, restart proof, outcome, and approved postmortem exist.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from eventlens.agents.external import ExternalAgentReport
from eventlens.agents.external_handoff import ExternalHandoffState, ExternalHandoffStatus
from eventlens.committee.packet import DecisionPacket
from eventlens.learning.models import ApprovedPostmortem, OutcomeRecord

from .research_evidence import ResearchEvidenceBundle


class MvpAcceptanceStatus(StrEnum):
    RELEASE_READY = "release_ready"
    BLOCKED = "blocked"


class MvpAcceptanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MvpAcceptanceStatus
    checks: dict[str, bool]
    blockers: tuple[str, ...] = ()

    @property
    def release_ready(self) -> bool:
        return self.status is MvpAcceptanceStatus.RELEASE_READY


def audit_mvp_outcome(
    *,
    evidence: ResearchEvidenceBundle,
    handoff: ExternalHandoffState,
    report: ExternalAgentReport | None,
    packet: DecisionPacket | None,
    qa_status: str | None,
    fresh_process_rehydrated: bool,
    outcome: OutcomeRecord | None,
    approved_postmortem: ApprovedPostmortem | None,
    first_live_packet_analyst_approved: bool,
) -> MvpAcceptanceResult:
    """Audit the actual PRD lifecycle without manufacturing missing stages."""

    facts = ResearchEvidenceBundle.model_validate(evidence.model_dump(mode="json"))
    history_ok = len(facts.prior_cards) >= 3 or any(
        item.component == "prior_cards" and item.kind.value == "insufficient_history"
        for item in facts.missingness
    )
    report_ok = (
        handoff.status is ExternalHandoffStatus.VALIDATED
        and report is not None
        and report.bundle_id == handoff.bundle_id
        and report.bundle_sha256 == handoff.bundle_sha256
        and report.event_id == facts.target.event_id
    )
    packet_ok = packet is not None and (
        packet.event_id == facts.target.event_id
        and packet.ticker.upper() == facts.target.ticker
        and packet.information_cutoff_at == facts.information_cutoff_at
    )
    qa_ok = qa_status in {"release_ready", "release_ready_with_warnings"}
    outcome_ok = outcome is not None and (
        outcome.event_id == facts.target.event_id
        and outcome.ticker.upper() == facts.target.ticker
        and outcome.collected_at > facts.information_cutoff_at
    )
    postmortem_ok = (
        approved_postmortem is not None and outcome is not None
        and approved_postmortem.outcome_id == outcome.outcome_id
        and approved_postmortem.approved_at >= outcome.collected_at
    )
    checks = {
        "canonical_research_evidence": facts.bundle_sha256 == facts.content_checksum(),
        "historical_context_gate": history_ok,
        "validated_agent_report": report_ok,
        "canonical_decision_packet": packet_ok,
        "independent_packet_release_qa": qa_ok,
        "fresh_process_rehydration": fresh_process_rehydrated,
        "post_event_outcome": outcome_ok,
        "approved_postmortem": postmortem_ok,
        "first_live_packet_analyst_review": first_live_packet_analyst_approved,
    }
    blockers = tuple(name for name, passed in checks.items() if not passed)
    return MvpAcceptanceResult(
        status=(MvpAcceptanceStatus.RELEASE_READY if not blockers else MvpAcceptanceStatus.BLOCKED),
        checks=checks,
        blockers=blockers,
    )


__all__ = ["MvpAcceptanceResult", "MvpAcceptanceStatus", "audit_mvp_outcome"]
