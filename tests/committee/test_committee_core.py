from datetime import date, datetime, timezone

import pytest

from eventlens.committee.models import (
    CanonicalMetric,
    CommitteeRole,
    ClaimSupportStatus,
    DeskClaim,
    DeskMandate,
    DeskReport,
    EscalationLevel,
    EvidenceReference,
    EvidenceSnapshot,
    HistoricalContextManifestEntry,
    InformationKind,
    RiskAssessment,
)
from eventlens.committee.orchestration import (
    build_disagreement_register,
    risk_vetoes_decision,
    run_independent_desks,
)
from eventlens.committee.packet import (
    DecisionPacket,
    PacketStatement,
    ProvenanceRecord,
    SynthesisStatus,
)
from eventlens.domain.events import EventTiming


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _historical_manifest(card_id: str = "card-1", event_id: str = "DEMO:2026Q1"):
    return HistoricalContextManifestEntry(
        card_id=card_id,
        event_id=event_id,
        ticker="DEMO",
        event_date=date(2026, 4, 28),
        knowledge_cutoff_at=NOW,
        content_sha256="b" * 64,
        executive_summary="capacity remained a key variable",
        key_messages=("management cited demand",),
    )


def test_historical_manifest_is_typed_and_utc_normalized():
    entry = _historical_manifest()
    assert entry.knowledge_cutoff_at.tzinfo is timezone.utc
    assert entry.key_messages == ("management cited demand",)


def test_snapshot_rejects_duplicate_historical_events_and_future_cutoff():
    with pytest.raises(ValueError, match="identifiers must be unique"):
        EvidenceSnapshot(
            **_snapshot().model_dump(exclude={"historical_context_cards"}),
            historical_context_cards=(_historical_manifest(), _historical_manifest("card-2")),
        )
    with pytest.raises(ValueError, match="cutoff exceeds"):
        EvidenceSnapshot(
            **_snapshot().model_dump(exclude={"historical_context_cards"}),
            historical_context_cards=(
                _historical_manifest().model_copy(update={"knowledge_cutoff_at": NOW.replace(day=26)}),
            ),
        )


def _snapshot() -> EvidenceSnapshot:
    return EvidenceSnapshot(
        snapshot_id="snapshot-1", event_id="DEMO:2026Q2", ticker="DEMO", information_cutoff_at=NOW,
        reported_timing=EventTiming.UNKNOWN, effective_timing=EventTiming.AFTER_MARKET_CLOSE,
        provider_reported_release_at=None, effective_release_at=datetime(2026, 7, 29, 20, tzinfo=timezone.utc),
        release_timestamp_checksum="a" * 64,
        evidence=(EvidenceReference(source_id="transcript", passage_id="p1"),),
        canonical_metrics=(CanonicalMetric(metric_id="move", rendered_value="2.1%", calculation_version="v1", basis="historical"),),
    )


def _report(role: CommitteeRole, statement: str = "capacity is a material variable") -> DeskReport:
    input_adjudication_id = None
    if role is CommitteeRole.INVESTMENT_COMMITTEE:
        first_pass = tuple(
            _report(first_pass_role)
            for first_pass_role in CommitteeRole
            if first_pass_role
            not in {
                CommitteeRole.ADJUDICATOR,
                CommitteeRole.INVESTMENT_COMMITTEE,
            }
        )
        input_adjudication_id = build_disagreement_register(
            first_pass
        ).adjudication_id
    return DeskReport(
        report_id=f"report-{role.value}", run_id="run-1", role=role, snapshot_id="snapshot-1",
        prompt_version="p1", model="fixture", generated_at=NOW, conclusion="decision support only",
        claims=(DeskClaim(claim_id=f"capacity-{role.value}", proposition_id="capacity", topic_id="capacity",
                          statement=statement, kind=InformationKind.FACT,
                          evidence_references=(EvidenceReference(source_id="transcript", passage_id="p1"),)),),
        input_adjudication_id=input_adjudication_id,
    )


def _risk(report_id: str, escalation: EscalationLevel = EscalationLevel.REVIEW_REQUIRED) -> RiskAssessment:
    return RiskAssessment(
        report_id=report_id, escalation=escalation, reasons=("tested",),
        missing_critical_evidence=False, timing_uncertainty=True, stale_data=False,
        excessive_model_agreement=False, unstable_estimates=False, insufficient_comparable_events=False,
        unavailable_desks=False, cutoff_failure=False, unconfirmed_release_timestamp=True,
    )


def test_independent_desks_receive_only_snapshot_and_mandate() -> None:
    calls: list[tuple[str, str]] = []

    def runner(snapshot: EvidenceSnapshot, mandate: DeskMandate) -> DeskReport:
        calls.append((snapshot.snapshot_id, mandate.role.value))
        return _report(mandate.role)

    first_pass_roles = tuple(
        role for role in CommitteeRole
        if role not in {
            CommitteeRole.ADJUDICATOR,
            CommitteeRole.INVESTMENT_COMMITTEE,
        }
    )
    mandates = tuple(DeskMandate(role=role, mandate="independent first pass", prompt_version="p1")
                     for role in sorted(first_pass_roles, key=str))
    reports = run_independent_desks(_snapshot(), mandates, {mandate.role: runner for mandate in mandates})
    assert len(reports) == 5
    assert calls == [("snapshot-1", mandate.role.value) for mandate in mandates]
    assert reports == run_independent_desks(_snapshot(), mandates, {mandate.role: runner for mandate in mandates})


def test_independence_requires_complete_role_coverage() -> None:
    mandate = DeskMandate(role=CommitteeRole.QUANT_STRATEGY, mandate="quant", prompt_version="p1")
    with pytest.raises(ValueError, match="cover every"):
        run_independent_desks(_snapshot(), (mandate,), {mandate.role: lambda *_: _report(mandate.role)})


def test_disagreement_is_retained_and_risk_veto_is_explicit() -> None:
    register = build_disagreement_register((_report(CommitteeRole.QUANT_STRATEGY), _report(CommitteeRole.RISK_COMMITTEE, "capacity is not material")))
    assert register.disputed[0].proposition_id == "capacity"
    risk = _risk("report-risk_committee", EscalationLevel.VETO)
    assert risk_vetoes_decision(risk)


def test_facts_cannot_be_unattributed() -> None:
    with pytest.raises(ValueError, match="require evidence"):
        DeskClaim(claim_id="x", proposition_id="x", topic_id="x", statement="unsupported", kind=InformationKind.FACT)


def test_decision_packet_retains_reports_and_fact_labels() -> None:
    reports = tuple(
        _report(role)
        for role in CommitteeRole
        if role is not CommitteeRole.ADJUDICATOR
    )
    risk_report = next(
        report for report in reports
        if report.role is CommitteeRole.RISK_COMMITTEE
    )
    packet = DecisionPacket(
        packet_id="packet-1", created_at=NOW, event_id="DEMO:2026Q2", ticker="DEMO",
        reported_timing=EventTiming.UNKNOWN, effective_timing=EventTiming.AFTER_MARKET_CLOSE,
        information_cutoff_at=NOW, canonical_expected_move_status="unavailable", snapshot=_snapshot(),
        desk_reports=reports,
        risk_assessment=_risk(risk_report.report_id),
        disagreement_register=build_disagreement_register(reports),
        synthesis_status=SynthesisStatus.ESCALATED,
        provider_reported_release_at=None, effective_release_at=datetime(2026, 7, 29, 20, tzinfo=timezone.utc),
        release_timestamp_checksum="a" * 64,
        historical_event_outcomes=(PacketStatement(statement_id="history", text="historical fact", kind=InformationKind.FACT, source_report_id=risk_report.report_id, source_claim_id=f"capacity-{risk_report.role.value}"),),
        current_objective_market_context=(PacketStatement(statement_id="context", text="context fact", kind=InformationKind.FACT, source_report_id=risk_report.report_id, source_claim_id=f"capacity-{risk_report.role.value}"),),
        provenance_manifest=(ProvenanceRecord(provenance_id="snapshot", reference="snapshot-1", classifications=(InformationKind.FACT,)),),
        synthesis=(PacketStatement(statement_id="s1", text="retained fact", kind=InformationKind.FACT, source_report_id=risk_report.report_id, source_claim_id=f"capacity-{risk_report.role.value}"),),
    )
    assert risk_report in packet.desk_reports
    with pytest.raises(ValueError, match="originating desk"):
        DecisionPacket(**{**packet.model_dump(), "synthesis": (PacketStatement(statement_id="s2", text="bad", kind=InformationKind.FACT),)})


def test_semantic_proposition_disagreement_and_unsupported_draft_cannot_reach_packet() -> None:
    reports = tuple(_report(role) for role in CommitteeRole if role is not CommitteeRole.ADJUDICATOR)
    register = build_disagreement_register(reports)
    assert register.consensus_proposition_ids == ("capacity",)
    draft = DeskClaim(
        claim_id="draft", proposition_id="draft-proposition", topic_id="timing", statement="unverified claim",
        kind=InformationKind.FACT, support_status=ClaimSupportStatus.DRAFT_UNSUPPORTED,
    )
    draft_risk = _report(CommitteeRole.RISK_COMMITTEE).model_copy(update={"claims": (draft,)})
    all_reports = tuple(draft_risk if report.role is CommitteeRole.RISK_COMMITTEE else report for report in reports)
    updated_register = build_disagreement_register(all_reports)
    all_reports = tuple(
        report.model_copy(
            update={"input_adjudication_id": updated_register.adjudication_id}
        )
        if report.role is CommitteeRole.INVESTMENT_COMMITTEE
        else report
        for report in all_reports
    )
    packet_args = {
        "packet_id": "packet-draft", "created_at": NOW, "event_id": "DEMO:2026Q2", "ticker": "DEMO",
        "reported_timing": EventTiming.UNKNOWN, "effective_timing": EventTiming.AFTER_MARKET_CLOSE,
        "information_cutoff_at": NOW, "provider_reported_release_at": None,
        "effective_release_at": datetime(2026, 7, 29, 20, tzinfo=timezone.utc), "release_timestamp_checksum": "a" * 64,
        "canonical_expected_move_status": "unavailable", "snapshot": _snapshot(), "desk_reports": all_reports,
        "risk_assessment": _risk(draft_risk.report_id), "disagreement_register": updated_register,
        "synthesis_status": SynthesisStatus.ESCALATED,
        "historical_event_outcomes": (PacketStatement(statement_id="h", text="x", kind=InformationKind.FACT, source_report_id="report-earnings_research", source_claim_id="capacity-earnings_research"),),
        "current_objective_market_context": (PacketStatement(statement_id="c", text="x", kind=InformationKind.FACT, source_report_id="report-earnings_research", source_claim_id="capacity-earnings_research"),),
        "provenance_manifest": (ProvenanceRecord(provenance_id="p", reference="snapshot-1", classifications=(InformationKind.FACT,)),),
    }
    with pytest.raises(ValueError, match="unsupported draft"):
        DecisionPacket(**packet_args, synthesis=(PacketStatement(statement_id="draft-s", text="draft", kind=InformationKind.FACT, source_report_id=draft_risk.report_id, source_claim_id="draft"),))


def test_veto_blocks_normal_synthesis() -> None:
    reports = tuple(_report(role) for role in CommitteeRole if role is not CommitteeRole.ADJUDICATOR)
    risk_report = next(report for report in reports if report.role is CommitteeRole.RISK_COMMITTEE)
    with pytest.raises(ValueError, match="risk veto"):
        DecisionPacket(
            packet_id="veto", created_at=NOW, event_id="DEMO:2026Q2", ticker="DEMO",
            reported_timing=EventTiming.UNKNOWN, effective_timing=EventTiming.AFTER_MARKET_CLOSE,
            information_cutoff_at=NOW, provider_reported_release_at=None, effective_release_at=datetime(2026, 7, 29, 20, tzinfo=timezone.utc), release_timestamp_checksum="a" * 64,
            canonical_expected_move_status="unavailable", snapshot=_snapshot(), desk_reports=reports,
            risk_assessment=_risk(risk_report.report_id, EscalationLevel.VETO), disagreement_register=build_disagreement_register(reports), synthesis_status=SynthesisStatus.NORMAL,
            historical_event_outcomes=(PacketStatement(statement_id="h", text="x", kind=InformationKind.FACT, source_report_id=risk_report.report_id, source_claim_id=f"capacity-{risk_report.role.value}"),),
            current_objective_market_context=(PacketStatement(statement_id="c", text="x", kind=InformationKind.FACT, source_report_id=risk_report.report_id, source_claim_id=f"capacity-{risk_report.role.value}"),),
            provenance_manifest=(ProvenanceRecord(provenance_id="p", reference="snapshot-1", classifications=(InformationKind.FACT,)),),
        )


def test_snapshot_reference_validation_and_required_risk_flags() -> None:
    invalid_report = _report(CommitteeRole.QUANT_STRATEGY).model_copy(
        update={
            "claims": (
                DeskClaim(
                    claim_id="outside", proposition_id="outside", topic_id="outside", statement="outside",
                    kind=InformationKind.FACT,
                    evidence_references=(EvidenceReference(source_id="not-in-snapshot", passage_id="p"),),
                ),
            )
        }
    )
    mandates = tuple(
        DeskMandate(role=role, mandate="first pass", prompt_version="p1")
        for role in (
            CommitteeRole.EARNINGS_RESEARCH, CommitteeRole.GLOBAL_EQUITIES,
            CommitteeRole.QUANT_STRATEGY, CommitteeRole.TECHNICAL_PANEL, CommitteeRole.RISK_COMMITTEE,
        )
    )
    def runner(_: EvidenceSnapshot, mandate: DeskMandate) -> DeskReport:
        return invalid_report if mandate.role is CommitteeRole.QUANT_STRATEGY else _report(mandate.role)
    with pytest.raises(ValueError, match="outside the snapshot"):
        run_independent_desks(_snapshot(), mandates, {mandate.role: runner for mandate in mandates})
    with pytest.raises(ValueError):
        RiskAssessment(report_id="r", escalation=EscalationLevel.NONE)  # type: ignore[call-arg]


def test_packet_requires_investment_after_adjudication_and_matching_release_provenance() -> None:
    reports = tuple(_report(role) for role in CommitteeRole if role is not CommitteeRole.ADJUDICATOR)
    investment = next(report for report in reports if report.role is CommitteeRole.INVESTMENT_COMMITTEE)
    reports_without_adjudication = tuple(
        report.model_copy(update={"input_adjudication_id": None}) if report is investment else report
        for report in reports
    )
    risk_report = next(report for report in reports if report.role is CommitteeRole.RISK_COMMITTEE)
    common = dict(
        packet_id="after-adjudication", created_at=NOW, event_id="DEMO:2026Q2", ticker="DEMO",
        reported_timing=EventTiming.UNKNOWN, effective_timing=EventTiming.AFTER_MARKET_CLOSE,
        information_cutoff_at=NOW, provider_reported_release_at=None,
        effective_release_at=datetime(2026, 7, 29, 20, tzinfo=timezone.utc), release_timestamp_checksum="a" * 64,
        canonical_expected_move_status="unavailable", snapshot=_snapshot(), risk_assessment=_risk(risk_report.report_id),
        disagreement_register=build_disagreement_register(reports), synthesis_status=SynthesisStatus.ESCALATED,
        historical_event_outcomes=(PacketStatement(statement_id="h", text="x", kind=InformationKind.FACT, source_report_id=risk_report.report_id, source_claim_id=f"capacity-{risk_report.role.value}"),),
        current_objective_market_context=(PacketStatement(statement_id="c", text="x", kind=InformationKind.FACT, source_report_id=risk_report.report_id, source_claim_id=f"capacity-{risk_report.role.value}"),),
        provenance_manifest=(ProvenanceRecord(provenance_id="p", reference="snapshot-1", classifications=(InformationKind.FACT,)),),
    )
    with pytest.raises(ValueError, match="after the retained adjudication"):
        DecisionPacket(**common, desk_reports=reports_without_adjudication)
    with pytest.raises(ValueError, match="timing and checksum"):
        DecisionPacket(**{**common, "desk_reports": reports, "release_timestamp_checksum": "b" * 64})
