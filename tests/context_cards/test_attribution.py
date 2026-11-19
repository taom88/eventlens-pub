"""Evidence-backed, explicitly inferred reaction attribution."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AttributionCandidate,
    AttributionConfidence,
    AttributionWindow,
    EvidenceClass,
    EvidenceReceipt,
    HumanReviewReason,
    ReactionAttribution,
    SemanticClaim,
    context_card_json_schema,
    decay_attribution_confidence,
    validate_attribution,
)


def claim(claim_id: str, proposition: str, topic: str) -> SemanticClaim:
    excerpt = proposition
    source_ref = f"source:fixture:passage:{claim_id}"
    receipt = EvidenceReceipt(
        id=claim_id,
        evidence_class=EvidenceClass.SOURCE_NATIVE,
        claim=proposition,
        source_ref=source_ref,
    )
    return SemanticClaim(
        id=claim_id,
        proposition=proposition,
        source_ref=source_ref,
        location={
            "source_id": "fixture",
            "passage_id": claim_id,
            "char_start": 0,
            "char_end": len(excerpt),
        },
        topics=(topic,),
        quality="complete",
        verbatim_receipt=excerpt,
        verbatim_receipt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        evidence=receipt,
    )


def candidate(
    candidate_id: str,
    factor: str,
    semantic_claim: SemanticClaim,
    *,
    direction: str,
    importance: str,
) -> AttributionCandidate:
    return AttributionCandidate(
        id=candidate_id,
        factor=factor,
        direction=direction,
        importance=importance,
        rationale=f"{factor} is a candidate driver, not proven causation.",
        semantic_claim_ids=(semantic_claim.id,),
        evidence_receipt_ids=(semantic_claim.evidence.id,),
    )


def attribution(*candidates: AttributionCandidate, **overrides: object) -> ReactionAttribution:
    values: dict[str, object] = {
        "window": AttributionWindow.T_PLUS_1,
        "candidate_drivers": candidates,
        "confidence": AttributionConfidence.MEDIUM,
        "confidence_rationale": "Multiple receipt-backed signals support a cautious interpretation.",
    }
    values.update(overrides)
    return ReactionAttribution(**values)


def test_source_native_attribution_is_rejected() -> None:
    revenue = claim("claim_revenue", "Revenue exceeded the stated expectation.", "revenue")
    driver = candidate(
        "driver_revenue", "revenue_surprise", revenue, direction="positive", importance="high"
    )

    with pytest.raises(ValidationError, match="inferred"):
        attribution(driver, classification=EvidenceClass.SOURCE_NATIVE)
    with pytest.raises(ValidationError, match="INFERRED.*ATTRIBUTED"):
        AttributionCandidate(
            **{**driver.model_dump(), "evidence_class": EvidenceClass.SOURCE_NATIVE}
        )

    with pytest.raises(ValidationError, match="deterministic market causation"):
        AttributionCandidate(
            **{**driver.model_dump(), "rationale": "The stock fell because guidance was weak."}
        )

    schema = context_card_json_schema()
    assert schema["$defs"]["AttributionCandidate"]["properties"]["evidence_class"][
        "enum"
    ] == ["inferred", "attributed"]
    assert schema["$defs"]["ReactionAttribution"]["properties"]["classification"][
        "const"
    ] == "inferred"


def test_dominant_driver_requires_evidence_candidates_and_narrative_alternatives() -> None:
    revenue = claim("claim_revenue", "Revenue exceeded expectations.", "revenue")
    driver = candidate(
        "driver_revenue", "revenue_surprise", revenue, direction="positive", importance="high"
    )

    with pytest.raises(ValidationError, match="requires candidate and narrative alternatives"):
        attribution(driver, dominant_driver_id=driver.id)


def test_strong_revenue_and_weak_guidance_remain_distinct_candidates() -> None:
    revenue = claim("claim_revenue", "Revenue exceeded expectations.", "revenue")
    guidance = claim("claim_guidance", "Forward guidance was below expectations.", "guidance")
    revenue_driver = candidate(
        "driver_revenue", "revenue_surprise", revenue, direction="positive", importance="high"
    )
    guidance_driver = candidate(
        "driver_guidance", "weak_forward_guidance", guidance, direction="negative", importance="high"
    )
    result = attribution(
        revenue_driver,
        guidance_driver,
        dominant_driver_id=guidance_driver.id,
        alternative_explanations=("The strong revenue result may explain part of the move.",),
    )

    validated = validate_attribution(
        result,
        (revenue, guidance),
        (revenue.evidence, guidance.evidence),
    )

    assert [item.factor for item in validated.candidate_drivers] == [
        "revenue_surprise",
        "weak_forward_guidance",
    ]
    assert validated.dominant_driver_language == "likely_reaction_driver"


def test_every_candidate_must_resolve_to_matching_claims_and_receipts() -> None:
    revenue = claim("claim_revenue", "Revenue exceeded expectations.", "revenue")
    driver = candidate(
        "driver_revenue", "revenue_surprise", revenue, direction="positive", importance="high"
    )
    result = attribution(driver)

    with pytest.raises(ValueError, match="unresolved semantic claims"):
        validate_attribution(result, (), (revenue.evidence,))
    with pytest.raises(ValueError, match="unresolved evidence receipts"):
        validate_attribution(result, (revenue,), ())


def test_uncontrolled_t_plus_five_confidence_decays_from_immediate_window() -> None:
    assert decay_attribution_confidence(
        AttributionConfidence.HIGH,
        from_window=AttributionWindow.T_PLUS_1,
        to_window=AttributionWindow.T_PLUS_5,
    ) is AttributionConfidence.MEDIUM
    assert decay_attribution_confidence(
        AttributionConfidence.HIGH,
        from_window=AttributionWindow.T_PLUS_1,
        to_window=AttributionWindow.T_PLUS_5,
        explicit_adjustment_basis="QQQ-adjusted return with no overlapping issuer event.",
    ) is AttributionConfidence.HIGH


def test_uncontrolled_t_plus_five_cannot_retain_short_window_high_confidence() -> None:
    from eventlens.context_cards.attribution import validate_attribution_horizons

    revenue = claim("claim_revenue", "Revenue exceeded expectations.", "revenue")
    driver = candidate(
        "driver_revenue", "revenue_surprise", revenue, direction="positive", importance="high"
    )
    immediate = attribution(
        driver,
        window=AttributionWindow.T_PLUS_1,
        confidence=AttributionConfidence.HIGH,
    )
    uncontrolled_drift = attribution(
        driver,
        window=AttributionWindow.T_PLUS_5,
        confidence=AttributionConfidence.HIGH,
    )

    with pytest.raises(ValueError, match="must decay"):
        validate_attribution_horizons((immediate, uncontrolled_drift))


def test_material_dominance_and_source_limits_have_explicit_human_review_flags() -> None:
    revenue = claim("claim_revenue", "Revenue exceeded expectations.", "revenue")
    guidance = claim("claim_guidance", "Forward guidance was below expectations.", "guidance")
    drivers = (
        candidate(
            "driver_revenue", "revenue_surprise", revenue, direction="positive", importance="high"
        ),
        candidate(
            "driver_guidance", "weak_forward_guidance", guidance, direction="negative", importance="high"
        ),
    )
    with pytest.raises(ValidationError, match="require human review"):
        attribution(
            *drivers,
            dominant_driver_id="driver_guidance",
            alternative_explanations=("Revenue may have partly offset guidance.",),
            material=True,
        )

    result = attribution(
        *drivers,
        dominant_driver_id="driver_guidance",
        alternative_explanations=("Revenue may have partly offset guidance.",),
        material=True,
        source_access_limitations=("No licensed contemporaneous analyst notes.",),
        requires_human_review=True,
        human_review_reasons=(
            HumanReviewReason.MATERIAL_DOMINANT_DRIVER,
            HumanReviewReason.SOURCE_ACCESS_LIMITATIONS,
        ),
    )
    assert result.requires_human_review is True
