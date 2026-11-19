from __future__ import annotations

import hashlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

from eventlens.context_cards import (
    AccountingBasis,
    AnalyticalUse,
    ClaimLocation,
    ClaimQuality,
    EvidenceClass,
    EvidenceReceipt,
    FiscalPeriod,
    GrowthBasis,
    InformationNeed,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
    RetrievalCandidate,
    SemanticClaim,
    retrieve_for_slots,
    score_candidate,
    select_diverse_claims,
)


SLOT = "cloud_growth_reported"


def _claim(
    claim_id: str,
    proposition: str,
    *,
    period: FiscalPeriod | None = None,
    value: str | None = None,
    evidence_class: EvidenceClass = EvidenceClass.SOURCE_NATIVE,
) -> SemanticClaim:
    source_id = f"source-{claim_id}"
    passage_id = f"passage-{claim_id}"
    source_ref = f"source:{source_id}:passage:{passage_id}"
    metric = None
    if period is not None and value is not None:
        metric = MetricValue(
            metric=NormalizedMetric.GROWTH,
            value=Decimal(value),
            unit=MetricUnit.PERCENT,
            fiscal_period=period,
            accounting_basis=AccountingBasis.NOT_APPLICABLE,
            growth_basis=GrowthBasis.REPORTED,
            scope=MetricScope.PRODUCT,
        )
    return SemanticClaim(
        id=claim_id,
        proposition=proposition,
        source_ref=source_ref,
        location=ClaimLocation(
            source_id=source_id,
            passage_id=passage_id,
            char_start=0,
            char_end=len(proposition),
        ),
        topics=("cloud", "growth"),
        quality=ClaimQuality.COMPLETE,
        verbatim_receipt=proposition,
        verbatim_receipt_sha256=hashlib.sha256(proposition.encode()).hexdigest(),
        evidence=EvidenceReceipt(
            id=claim_id,
            evidence_class=evidence_class,
            claim=proposition,
            source_ref=source_ref,
        ),
        metric=metric,
    )


def _candidate(
    claim_id: str,
    proposition: str,
    *,
    period: FiscalPeriod | None = None,
    value: str | None = None,
    normalized_claim_key: str | None = None,
    conflict_evidence: bool = False,
    evidence_class: EvidenceClass = EvidenceClass.SOURCE_NATIVE,
    **changes: object,
) -> RetrievalCandidate:
    payload: dict[str, object] = {
        "claim": _claim(
            claim_id,
            proposition,
            period=period,
            value=value,
            evidence_class=evidence_class,
        ),
        "ontology_slots": (SLOT,),
        "fiscal_period": period,
        "normalized_claim_key": normalized_claim_key,
        "conflict_evidence": conflict_evidence,
        "source_authority": Decimal("0.9"),
        "semantic_relevance": Decimal("0.9"),
        "decision_relevance": Decimal("0.8"),
        "temporal_relevance": Decimal("0.9"),
        "numeric_specificity": Decimal("0.8"),
        "forward_information_value": Decimal("0.7"),
        "novelty": Decimal("0.8"),
    }
    payload.update(changes)
    return RetrievalCandidate(**payload)


def _need(**changes: object) -> InformationNeed:
    payload: dict[str, object] = {
        "ontology_slot": SLOT,
        "description": "Retrieve cloud reported growth.",
    }
    payload.update(changes)
    return InformationNeed(**payload)


def test_boilerplate_ranks_below_numeric_guidance_with_visible_breakdown() -> None:
    numeric = _candidate(
        "numeric",
        "cloud revenue grew 43% year over year.",
        period=FiscalPeriod(fiscal_year=2026, fiscal_quarter=2),
        value="43",
    )
    boilerplate = _candidate(
        "boilerplate",
        "cloud is part of our broad and trusted cloud portfolio.",
        numeric_specificity=Decimal("0"),
        forward_information_value=Decimal("0"),
        boilerplate=Decimal("1"),
    )

    numeric_score = score_candidate(numeric, _need())
    boilerplate_score = score_candidate(boilerplate, _need())

    assert numeric_score.total > boilerplate_score.total
    assert boilerplate_score.boilerplate == Decimal("0.30")
    assert set(numeric_score.model_dump()) >= {
        "source_authority",
        "semantic_relevance",
        "decision_relevance",
        "temporal_relevance",
        "numeric_specificity",
        "forward_information_value",
        "novelty",
        "boilerplate",
        "metric_definition_only",
        "truncation",
        "duplicate_information",
        "low_thesis_relevance",
        "unresolved_speaker",
    }


@pytest.mark.parametrize("penalty", ["truncation", "duplicate_information", "unresolved_speaker"])
def test_material_penalties_reduce_score(penalty: str) -> None:
    baseline = _candidate("baseline", "cloud revenue grew 43% year over year.")
    penalized = _candidate(
        "penalized",
        "cloud revenue grew approximately 43% year over year.",
        **{penalty: Decimal("1")},
    )

    assert score_candidate(penalized, _need()).total < score_candidate(baseline, _need()).total


def test_retrieval_is_need_first_covers_slots_and_latest_quarter_wins_current_state() -> None:
    old = _candidate(
        "old",
        "cloud revenue grew 50% year over year.",
        period=FiscalPeriod(fiscal_year=2025, fiscal_quarter=4),
        value="50",
        source_authority=Decimal("1"),
        semantic_relevance=Decimal("1"),
    )
    latest = _candidate(
        "latest",
        "cloud revenue grew 39% year over year.",
        period=FiscalPeriod(fiscal_year=2026, fiscal_quarter=1),
        value="39",
        source_authority=Decimal("0.5"),
        semantic_relevance=Decimal("0.5"),
    )
    missing_need = InformationNeed(
        ontology_slot="capex", description="Retrieve current capex.", current_state=True
    )

    retrieved = retrieve_for_slots(
        (_need(current_state=True, max_results=2), missing_need),
        (old, latest),
        ontology=SimpleNamespace(kpis_by_id={SLOT: None, "capex": None}),
    )

    assert tuple(retrieved) == (SLOT, "capex")
    assert [candidate.id for candidate in retrieved[SLOT]] == ["latest", "old"]
    assert retrieved["capex"] == ()


def test_diversity_prefers_new_information_and_caps_duplicate_receipts() -> None:
    candidates = tuple(
        _candidate(
            f"same-{index}",
            f"Receipt {index} says cloud growth was 43%.",
            normalized_claim_key="cloud_growth_reported|FY2026_Q2|43|percent",
        )
        for index in range(4)
    ) + (
        _candidate(
            "capacity",
            "Available AI capacity constrained cloud growth this quarter.",
            normalized_claim_key="cloud_capacity_constraint|FY2026_Q2",
            source_authority=Decimal("0.6"),
        ),
    )

    selected = select_diverse_claims(candidates, _need(max_results=5))

    duplicate_count = sum(
        candidate.normalized_claim_key == "cloud_growth_reported|FY2026_Q2|43|percent"
        for candidate in selected
    )
    assert duplicate_count == 2
    assert selected[1].id == "capacity"


def test_conflict_evidence_can_exceed_normalized_claim_receipt_cap() -> None:
    candidates = tuple(
        _candidate(
            f"conflict-{index}",
            f"Conflict receipt {index} reports cloud growth as 43%.",
            normalized_claim_key="cloud_growth_conflict",
            conflict_evidence=index == 2,
        )
        for index in range(3)
    )

    selected = select_diverse_claims(candidates, _need(max_results=3))

    assert len(selected) == 3


def test_evidence_permission_fails_closed_and_ties_are_deterministic() -> None:
    reconstructed = _candidate(
        "reconstructed",
        "cloud revenue grew 43% year over year.",
        evidence_class=EvidenceClass.RECONSTRUCTED,
    )
    autonomous = _need(analytical_use=AnalyticalUse.AUTONOMOUS_DECISION)
    rejected = score_candidate(reconstructed, autonomous)
    assert not rejected.eligible
    assert rejected.total == 0
    assert rejected.rejection_reason is not None

    first = _candidate("a", "cloud revenue grew 41% year over year.")
    second = _candidate("b", "cloud revenue grew 42% year over year.")
    forward = select_diverse_claims((second, first), _need(max_results=2))
    reverse = select_diverse_claims((first, second), _need(max_results=2))
    assert [candidate.id for candidate in forward] == ["a", "b"]
    assert forward == reverse


def test_unknown_ontology_slot_is_rejected() -> None:
    need = InformationNeed(ontology_slot="not_a_known_slot", description="Unknown slot.")
    with pytest.raises(ValueError, match="unknown ontology slots"):
        retrieve_for_slots((need,), (), ontology=SimpleNamespace(kpis_by_id={}))
