"""Typed decision-support packet that retains dissent and provenance."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.domain.events import EventTiming

from .models import (
    ClaimSupportStatus,
    CommitteeRole,
    DeskReport,
    DisagreementRegister,
    EvidenceGap,
    EvidenceSnapshot,
    InformationKind,
    RiskAssessment,
)
from .orchestration import (
    build_disagreement_register,
    validate_report_against_snapshot,
)


class SynthesisStatus(StrEnum):
    NORMAL = "normal"
    ESCALATED = "escalated"
    VETOED = "vetoed"


class ProvenanceRecord(BaseModel):
    """A classified provenance entry rather than an opaque string list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance_id: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    classifications: tuple[InformationKind, ...] = Field(min_length=1)


class PacketStatement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: InformationKind
    source_report_id: str | None = None
    source_claim_id: str | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    conditions: tuple[str, ...] = Field(min_length=1)
    statements: tuple[PacketStatement, ...] = ()


class DecisionPacket(BaseModel):
    """An auditable synthesis; it cannot silently collapse desk-level dissent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    packet_id: str = Field(min_length=1)
    created_at: datetime
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    reported_timing: EventTiming
    effective_timing: EventTiming
    information_cutoff_at: datetime
    provider_reported_release_at: datetime | None = None
    effective_release_at: datetime
    release_timestamp_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_expected_move_status: str = Field(min_length=1)
    snapshot: EvidenceSnapshot
    desk_reports: tuple[DeskReport, ...] = Field(min_length=1)
    risk_assessment: RiskAssessment
    disagreement_register: DisagreementRegister
    synthesis_status: SynthesisStatus
    historical_event_outcomes: tuple[PacketStatement, ...] = Field(min_length=1)
    current_objective_market_context: tuple[PacketStatement, ...] = Field(min_length=1)
    scenarios: tuple[Scenario, ...] = ()
    evidence_gaps: tuple[EvidenceGap, ...] = ()
    refresh_triggers: tuple[str, ...] = ()
    provenance_manifest: tuple[ProvenanceRecord, ...] = Field(min_length=1)
    synthesis: tuple[PacketStatement, ...] = ()
    decision_support_notice: str = (
        "Decision support only; this packet is not personalized financial advice or trade execution."
    )

    @field_validator(
        "created_at", "information_cutoff_at", "provider_reported_release_at", "effective_release_at"
    )
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("packet timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def packet_preserves_snapshot_and_dissent(self) -> "DecisionPacket":
        if self.snapshot.event_id != self.event_id or self.snapshot.ticker != self.ticker:
            raise ValueError("packet event identity must match the evidence snapshot")
        if (
            self.snapshot.reported_timing is not self.reported_timing
            or self.snapshot.effective_timing is not self.effective_timing
            or self.snapshot.provider_reported_release_at != self.provider_reported_release_at
            or self.snapshot.effective_release_at != self.effective_release_at
            or self.snapshot.release_timestamp_checksum != self.release_timestamp_checksum
        ):
            raise ValueError("packet release timing and checksum must match the snapshot")
        report_ids = {report.report_id for report in self.desk_reports}
        roles = {report.role for report in self.desk_reports}
        required_roles = {
            CommitteeRole.EARNINGS_RESEARCH,
            CommitteeRole.GLOBAL_EQUITIES,
            CommitteeRole.QUANT_STRATEGY,
            CommitteeRole.TECHNICAL_PANEL,
            CommitteeRole.RISK_COMMITTEE,
            CommitteeRole.INVESTMENT_COMMITTEE,
        }
        if len(report_ids) != len(self.desk_reports) or len(roles) != len(self.desk_reports):
            raise ValueError("decision packet cannot duplicate desk reports or desk roles")
        if roles != required_roles:
            raise ValueError("decision packet must retain every desk role")
        for report in self.desk_reports:
            validate_report_against_snapshot(report, self.snapshot)
        if self.risk_assessment.report_id not in report_ids:
            raise ValueError("risk assessment must reference a retained desk report")
        risk_report = next(
            report
            for report in self.desk_reports
            if report.report_id == self.risk_assessment.report_id
        )
        if risk_report.role is not CommitteeRole.RISK_COMMITTEE:
            raise ValueError("risk assessment must reference the risk committee report")
        investment_report = next(
            report for report in self.desk_reports if report.role is CommitteeRole.INVESTMENT_COMMITTEE
        )
        if investment_report.input_adjudication_id != self.disagreement_register.adjudication_id:
            raise ValueError("investment committee report must be produced after the retained adjudication")
        first_pass = tuple(
            report
            for report in self.desk_reports
            if report.role is not CommitteeRole.INVESTMENT_COMMITTEE
        )
        if build_disagreement_register(first_pass) != self.disagreement_register:
            raise ValueError(
                "disagreement register does not match the retained first-pass reports"
            )
        if self.risk_assessment.escalation.value == "veto":
            if self.synthesis_status is SynthesisStatus.NORMAL:
                raise ValueError("risk veto prohibits normal investment synthesis")
            if self.synthesis:
                raise ValueError("risk veto prohibits investment synthesis content")
        elif self.synthesis_status is SynthesisStatus.VETOED:
            raise ValueError("a non-veto risk assessment cannot produce a vetoed packet")
        participating = {
            report_id
            for disagreement in self.disagreement_register.disputed
            for report_id in disagreement.participating_reports
        }
        if not participating.issubset(report_ids):
            raise ValueError("disagreement register references a missing desk report")
        claim_index = {
            (report.report_id, claim.claim_id): claim
            for report in self.desk_reports
            for claim in report.claims
        }
        for statement in (
            *self.synthesis,
            *self.historical_event_outcomes,
            *self.current_objective_market_context,
            *(statement for scenario in self.scenarios for statement in scenario.statements),
        ):
            if statement.kind in {
                InformationKind.FACT,
                InformationKind.MODEL_INTERPRETATION,
            }:
                if statement.source_report_id is None or statement.source_claim_id is None:
                    raise ValueError("packet facts and interpretations must identify their originating desk claim")
                claim = claim_index.get((statement.source_report_id, statement.source_claim_id))
                if claim is None:
                    raise ValueError("packet statement references a missing desk claim")
                if claim.support_status is ClaimSupportStatus.DRAFT_UNSUPPORTED:
                    raise ValueError("unsupported draft claims cannot enter packet facts or interpretations")
        return self
