from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from eventlens.context_cards import AnalyticalUse, ContextCardV4, EvidenceClass, EvidenceReceipt
from eventlens.context_cards.qc import (
    ConfidenceComponents,
    QCCode,
    PublicationStatus,
    aggregate_confidence,
    detect_staleness,
    validate_card,
)
from eventlens.context_cards import qc


def _minimal_v4_card() -> dict[str, object]:
    generated_at = "2026-09-09T03:42:16+00:00"
    return {
        "schema_version": "eventlens.context_card.v4",
        "identity": {"ticker": "DEMO", "company": "Example Systems Corporation", "event": {"type": "earnings", "fiscal_period": "FY2027_Q1", "event_datetime": None, "timezone": None, "event_datetime_status": "unknown"}, "card": {"version": 4, "generated_at": generated_at, "as_of": "2026-09-09T00:00:00+00:00", "sha256": None, "status": "draft"}},
        "temporal_state": {"historical_event_cutoff": "2026-09-08T20:00:00+00:00", "observations": {}},
        "decision_context": {"decision_type": "pre_earnings_event_trade", "reference_price": {"value": None, "status": "unknown"}, "entry_threshold": {"value": None, "status": "not_computed"}, "currency": "USD", "long_term_view_explicitly_separate": True, "decision_status": "not_evaluated"},
        "current_pre_event_state": {},
        "company_ontology": {"ontology_id": "demo_kpi_ontology", "version": "1.0.0-proposed.1", "reference": "package://eventlens.context_cards.ontologies/DEMO.yaml"},
        "historical_quarters": [], "management_guidance_ledger": [], "consensus_state": {},
        "market_reaction_history": [], "thesis_state": {}, "scenario_model": {}, "decision": {},
        "evidence_registry": {}, "conflicts": [], "quality_control": {},
        "provenance": {"schema_version": "eventlens.context_card.v4", "ontology_version": "1.0.0-proposed.1", "generated_at": generated_at},
    }


def test_critical_low_confidence_caps_aggregate() -> None:
    components = ConfidenceComponents(
        factual_integrity=Decimal("0.10"),
        temporal_integrity=Decimal("1"),
        accounting_comparability=Decimal("1"),
        consensus_quality=Decimal("1"),
        attribution_quality=Decimal("1"),
        decision_calibration=Decimal("1"),
    )

    assert aggregate_confidence(components) == Decimal("0.5900")


def test_staleness_reports_new_quarter_event_change_and_aged_consensus() -> None:
    # ``model_construct`` is intentional: staleness needs only this immutable
    # envelope subset and is independently unit-tested from schema validation.
    card = ContextCardV4.model_construct(
        current_pre_event_state=SimpleNamespace(latest_reported_fiscal_period="FY2026_Q2"),
        identity=SimpleNamespace(event=SimpleNamespace(event_datetime=datetime(2026, 1, 28, 21, tzinfo=timezone.utc))),
    )

    issues = detect_staleness(
        card,
        now=datetime(2026, 2, 20, tzinfo=timezone.utc),
        latest_completed_fiscal_period="FY2026_Q3",
        current_event_datetime=datetime(2026, 1, 29, 21, tzinfo=timezone.utc),
        latest_consensus_timestamp=datetime(2026, 1, 28, tzinfo=timezone.utc),
    )

    assert {issue.code for issue in issues} == {
        QCCode.NEW_QUARTER_AVAILABLE,
        QCCode.EVENT_DATE_CHANGED,
        QCCode.CONSENSUS_AGED,
    }
    assert all(issue.severity.value == "warning" for issue in issues)


def test_validate_card_strictly_blocks_missing_required_invalid_unit_and_invalid_period() -> None:
    missing = _minimal_v4_card()
    del missing["identity"]  # type: ignore[index]
    assert QCCode.REQUIRED_FIELD_MISSING in {issue.code for issue in validate_card(missing).blockers}

    invalid_unit = _minimal_v4_card()
    invalid_unit["current_pre_event_state"] = {"latest_actuals": {"revenue": {"metric": "revenue", "value": "1", "unit": "not_a_unit", "fiscal_period": {"fiscal_year": 2026, "fiscal_quarter": 1}, "accounting_basis": "gaap", "growth_basis": "not_applicable", "scope": "consolidated"}}}
    assert QCCode.UNIT_INVALID in {issue.code for issue in validate_card(invalid_unit).blockers}

    invalid_period = copy.deepcopy(invalid_unit)
    invalid_period["current_pre_event_state"]["latest_actuals"]["revenue"]["unit"] = "USD_billion"  # type: ignore[index]
    invalid_period["current_pre_event_state"]["latest_actuals"]["revenue"]["fiscal_period"]["fiscal_quarter"] = 5  # type: ignore[index]
    assert QCCode.PERIOD_INVALID in {issue.code for issue in validate_card(invalid_period).blockers}


def test_neutral_card_reaches_reviewed_and_each_warning_remains_nonblocking() -> None:
    report = validate_card(
        _minimal_v4_card(),
        warning_signals={
            "consensus_source_count": 1,
            "absent_tier_4_kpis": ["xbox"],
            "macro_overlap_t_plus_5": True,
            "secondary_transcript_provider": True,
        },
    )

    assert report.publication_status is PublicationStatus.REVIEWED
    assert not report.blockers
    assert {issue.code for issue in report.warnings} == {
        QCCode.ONE_CONSENSUS_SOURCE,
        QCCode.TIER_4_KPI_ABSENT,
        QCCode.MACRO_OVERLAP_T_PLUS_5,
        QCCode.SECONDARY_TRANSCRIPT_PROVIDER,
    }


def test_medium_reaction_attribution_is_a_warning_not_a_publication_blocker() -> None:
    issues = []
    card = SimpleNamespace(
        evidence_registry=SimpleNamespace(semantic_claims=[]),
        market_reaction_history=[
            SimpleNamespace(
                attributions=[
                    SimpleNamespace(
                        confidence=SimpleNamespace(value="medium"),
                        candidate_drivers=[],
                    )
                ]
            )
        ],
        historical_quarters=[],
    )

    qc._validate_transcript_and_attribution(card, set(), issues)

    assert [(issue.code, issue.severity.value) for issue in issues] == [
        (QCCode.MEDIUM_REACTION_ATTRIBUTION, "warning")
    ]


def test_hard_qc_helpers_fail_closed_for_each_non_schema_failure_mode() -> None:
    issues = []
    derived = EvidenceReceipt.model_construct(
        id="derived", evidence_class=EvidenceClass.DERIVED, claim="derived claim",
        input_receipt_ids=(), operands_validated=False,
    )
    reconstructed = EvidenceReceipt(
        id="reconstructed", evidence_class=EvidenceClass.RECONSTRUCTED,
        claim="reconstructed claim", source_ref="source:reconstructed",
    )
    qc._validate_receipt_graph(derived, {}, issues)
    qc._validate_receipt_graph(
        EvidenceReceipt.model_construct(
            id="dangling", evidence_class=EvidenceClass.DERIVED, claim="dangling claim",
            input_receipt_ids=("missing",), operands_validated=True,
        ),
        {}, issues,
    )
    temporal_card = SimpleNamespace(
        evidence_registry=SimpleNamespace(receipts_by_id={"reconstructed": reconstructed}),
        temporal_state=SimpleNamespace(observations={
            "reconstructed": SimpleNamespace(
                available_before_event=SimpleNamespace(value="unavailable"),
                classification="reconstructed", temporal_confidence="low",
            )
        }),
    )
    qc._validate_temporal_and_permissions(
        temporal_card,
        {"reconstructed": reconstructed},
        {"reconstructed": (
            AnalyticalUse.BACKTEST_FEATURE,
            AnalyticalUse.AUTONOMOUS_DECISION,
        ), "unknown": ("bad_use",)},
        issues,
    )
    qc._validate_eps_surprises(
        SimpleNamespace(historical_quarters=[SimpleNamespace(surprise_layers=SimpleNamespace(
            eps_vs_sell_side=SimpleNamespace(
                normalized=SimpleNamespace(status="blocked"),
                estimate=SimpleNamespace(accounting_basis=SimpleNamespace(value="unknown")),
            )
        ))]),
        issues,
    )
    qc._validate_transcript_and_attribution(
        SimpleNamespace(
            evidence_registry=SimpleNamespace(semantic_claims=[SimpleNamespace(
                id="speaker", topics=("unresolved_management_speaker_numeric_guidance",)
            )]),
            market_reaction_history=[SimpleNamespace(attributions=[SimpleNamespace(
                confidence=SimpleNamespace(value="high"),
                candidate_drivers=[SimpleNamespace(
                    id="attribution", evidence_class=EvidenceClass.SOURCE_NATIVE,
                    semantic_claim_ids=("missing",),
                )],
            )])],
            historical_quarters=[],
        ),
        {"speaker"},
        issues,
    )
    qc._validate_tier_1_reasons({"cloud": None}, issues)
    qc._validate_decision_trace(
        SimpleNamespace(
            decision=SimpleNamespace(model_dump=lambda **_: {"status": "computed", "action": "buy", "threshold_methodology": None, "trace": None}),
            decision_context=SimpleNamespace(
                horizon_start=None, horizon_end=None,
                entry_threshold=SimpleNamespace(status="computed"),
            ),
        ),
        issues,
    )

    assert {
        QCCode.DANGLING_EVIDENCE_RECEIPT,
        QCCode.DERIVED_OPERANDS_UNVALIDATED,
        QCCode.RECONSTRUCTED_BACKTEST_INPUT,
        QCCode.CUTOFF_LEAKAGE,
        QCCode.EVIDENCE_PERMISSION_DENIED,
        QCCode.SURPRISE_COMPARABILITY_UNVALIDATED,
        QCCode.CONSENSUS_BASIS_UNVERIFIED,
        QCCode.TRANSCRIPT_SPEAKER_UNRESOLVED,
        QCCode.ATTRIBUTION_REPRESENTED_AS_FACT,
        QCCode.TIER_1_MISSING_REASON,
        QCCode.DECISION_HORIZON_MISSING,
        QCCode.DECISION_THRESHOLD_METHODOLOGY_MISSING,
        QCCode.DECISION_TRACE_MISSING,
    } <= {issue.code for issue in issues}
    assert all(issue.blocking for issue in issues)
