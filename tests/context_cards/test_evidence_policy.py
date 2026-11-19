from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AnalyticalPermission,
    AnalyticalUse,
    ContextCardV4,
    EvidenceClass,
    EvidencePolicy,
    EvidenceReceipt,
    EvidenceRegistry,
    NewsEvidenceReceipt,
    NewsSignalBasis,
    permission_for,
    require_permission,
)


GENERATED_AT = "2026-09-09T03:42:16.107760+00:00"


def _minimal_card() -> dict[str, object]:
    return {
        "identity": {
            "ticker": "DEMO",
            "company": "Example Systems Corporation",
            "event": {"fiscal_period": "FY2027_Q1"},
            "card": {
                "generated_at": GENERATED_AT,
                "as_of": "2026-09-09T00:00:00+00:00",
            },
        },
        "temporal_state": {
            "historical_event_cutoff": "2026-09-08T20:00:00+00:00"
        },
        "decision_context": {"currency": "USD"},
        "current_pre_event_state": {},
        "company_ontology": {
            "ontology_id": "demo_kpi_ontology",
            "version": "1.0.0-proposed.1",
            "reference": "package://eventlens.context_cards.ontologies/DEMO.yaml",
        },
        "historical_quarters": [],
        "management_guidance_ledger": [],
        "consensus_state": {},
        "market_reaction_history": [],
        "thesis_state": {},
        "scenario_model": {},
        "decision": {},
        "evidence_registry": {},
        "conflicts": [],
        "quality_control": {},
        "provenance": {
            "ontology_version": "1.0.0-proposed.1",
            "generated_at": GENERATED_AT,
        },
    }


def _native(receipt_id: str = "ev_native") -> EvidenceReceipt:
    return EvidenceReceipt(
        id=receipt_id,
        evidence_class=EvidenceClass.SOURCE_NATIVE,
        claim="Issuer reported cloud growth of 43 percent.",
        source_ref="source:demo-release:paragraph-17",
        temporal_context_ref="temporal:demo-release",
    )


def test_reconstructed_consensus_is_context_only() -> None:
    reconstructed = EvidenceReceipt(
        id="ev_reconstructed_consensus",
        evidence_class=EvidenceClass.RECONSTRUCTED,
        claim="A present-day provider response reports historical consensus.",
        source_ref="source:provider-snapshot:consensus",
    )

    assert permission_for(
        reconstructed, AnalyticalUse.DESCRIPTIVE_CONTEXT
    ) is AnalyticalPermission.ALLOWED
    for prohibited_use in (
        AnalyticalUse.SURPRISE_COMPUTATION,
        AnalyticalUse.BACKTEST_FEATURE,
        AnalyticalUse.BACKTEST_LABEL,
        AnalyticalUse.AUTONOMOUS_DECISION,
    ):
        assert permission_for(
            reconstructed, prohibited_use
        ) is AnalyticalPermission.PROHIBITED
        with pytest.raises(PermissionError):
            require_permission(reconstructed, prohibited_use)


def test_derived_evidence_requires_validated_resolvable_operand_receipts() -> None:
    with pytest.raises(ValidationError, match="validated operand receipts"):
        EvidenceReceipt(
            id="ev_surprise",
            evidence_class=EvidenceClass.DERIVED,
            claim="Revenue exceeded consensus.",
            input_receipt_ids=("ev_actual", "ev_consensus"),
        )

    derived = EvidenceReceipt(
        id="ev_surprise",
        evidence_class=EvidenceClass.DERIVED,
        claim="Revenue exceeded consensus.",
        input_receipt_ids=("ev_actual", "ev_consensus"),
        operands_validated=True,
    )
    assert permission_for(
        derived, AnalyticalUse.SURPRISE_COMPUTATION
    ) is AnalyticalPermission.PROHIBITED

    registry = EvidenceRegistry(
        receipts=(_native("ev_actual"), _native("ev_consensus"), derived)
    )
    assert permission_for(
        derived,
        AnalyticalUse.SURPRISE_COMPUTATION,
        registry=registry,
    ) is AnalyticalPermission.ALLOWED


def test_derived_evidence_cannot_launder_a_reconstructed_operand() -> None:
    actual = _native("ev_actual")
    reconstructed_consensus = EvidenceReceipt(
        id="ev_consensus",
        evidence_class=EvidenceClass.RECONSTRUCTED,
        claim="Present-day response for an historical consensus estimate.",
        source_ref="source:provider-current-response",
    )
    derived = EvidenceReceipt(
        id="ev_surprise",
        evidence_class=EvidenceClass.DERIVED,
        claim="Actual less reconstructed consensus.",
        input_receipt_ids=(actual.id, reconstructed_consensus.id),
        operands_validated=True,
    )
    registry = EvidenceRegistry(receipts=(actual, reconstructed_consensus, derived))

    assert permission_for(
        derived,
        AnalyticalUse.DESCRIPTIVE_CONTEXT,
        registry=registry,
    ) is AnalyticalPermission.ALLOWED
    assert permission_for(
        derived,
        AnalyticalUse.SURPRISE_COMPUTATION,
        registry=registry,
    ) is AnalyticalPermission.PROHIBITED


def test_inferred_evidence_requires_confidence_and_traceable_inputs() -> None:
    with pytest.raises(ValidationError, match="requires confidence"):
        EvidenceReceipt(
            id="ev_inference",
            evidence_class=EvidenceClass.INFERRED,
            claim="Capacity constraints may have limited cloud growth.",
            input_receipt_ids=("ev_native",),
        )
    with pytest.raises(ValidationError, match="traceable input receipts"):
        EvidenceReceipt(
            id="ev_inference",
            evidence_class=EvidenceClass.INFERRED,
            claim="Capacity constraints may have limited cloud growth.",
            confidence="0.65",
        )

    inference = EvidenceReceipt(
        id="ev_inference",
        evidence_class=EvidenceClass.INFERRED,
        claim="Capacity constraints may have limited cloud growth.",
        input_receipt_ids=("ev_native",),
        confidence="0.65",
    )
    assert permission_for(
        inference,
        AnalyticalUse.SUPPORT_INTERPRETATION,
    ) is AnalyticalPermission.PROHIBITED
    registry = EvidenceRegistry(receipts=(_native(), inference))
    assert permission_for(
        inference,
        AnalyticalUse.SUPPORT_INTERPRETATION,
        registry=registry,
    ) is AnalyticalPermission.ALLOWED
    assert permission_for(
        inference, AnalyticalUse.BACKTEST_LABEL
    ) is AnalyticalPermission.PROHIBITED


def test_attributed_evidence_cannot_masquerade_or_claim_causation() -> None:
    attributed = EvidenceReceipt(
        id="ev_attribution",
        evidence_class=EvidenceClass.ATTRIBUTED,
        claim="Management attributed margin pressure to AI infrastructure.",
        source_ref="source:transcript:cfo-turn-12",
    )
    assert permission_for(
        attributed, AnalyticalUse.SUPPORT_INTERPRETATION
    ) is AnalyticalPermission.ALLOWED
    assert permission_for(
        attributed, AnalyticalUse.REPRESENT_AS_SOURCE_NATIVE
    ) is AnalyticalPermission.PROHIBITED
    assert permission_for(
        attributed, AnalyticalUse.PROVE_CAUSATION
    ) is AnalyticalPermission.PROHIBITED

    with pytest.raises(ValidationError, match="cannot prove causation"):
        EvidenceReceipt(
            **{**attributed.model_dump(), "claims_causation": True}
        )


def test_missing_policy_entries_fail_closed() -> None:
    empty_policy = EvidencePolicy()

    assert permission_for(
        EvidenceClass.SOURCE_NATIVE,
        AnalyticalUse.DESCRIPTIVE_CONTEXT,
        policy=empty_policy,
    ) is AnalyticalPermission.PROHIBITED
    assert permission_for(
        EvidenceClass.SOURCE_NATIVE,
        AnalyticalUse.DESCRIPTIVE_CONTEXT,
        policy=None,
    ) is AnalyticalPermission.PROHIBITED


def test_card_embeds_typed_receipts_and_rejects_raw_provider_bodies() -> None:
    payload = copy.deepcopy(_minimal_card())
    payload["evidence_registry"] = {
        "receipts": [_native().model_dump(mode="json")]
    }

    card = ContextCardV4.model_validate(payload)
    assert card.evidence_registry.receipts[0].evidence_class is EvidenceClass.SOURCE_NATIVE

    payload["evidence_registry"]["raw_provider_body"] = {"data": "opaque payload"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ContextCardV4.model_validate(payload)


def test_card_rejects_duplicate_and_unresolved_receipt_ids() -> None:
    duplicate = EvidenceRegistry(receipts=(_native(),))
    with pytest.raises(ValidationError, match="ids must be unique"):
        EvidenceRegistry(receipts=duplicate.receipts + duplicate.receipts)

    inferred = EvidenceReceipt(
        id="ev_inference",
        evidence_class=EvidenceClass.INFERRED,
        claim="A traceable but unresolved inference.",
        input_receipt_ids=("ev_missing",),
        confidence="0.5",
    )
    with pytest.raises(ValidationError, match="unresolved input receipts"):
        EvidenceRegistry(receipts=(inferred,))


def test_news_evidence_receipt_is_rights_aware_metadata_only() -> None:
    receipt = NewsEvidenceReceipt(
        id="ev_news_demo_1",
        evidence_class=EvidenceClass.RECONSTRUCTED,
        claim="A provider metadata record reported a Example Systems earnings headline.",
        source_ref="news:alpha_vantage:av-news-1",
        provider_name="alpha_vantage",
        provider_article_id="av-news-1",
        ticker="demo",
            query_start="2026-07-01",
            query_end="2026-07-02",
            cutoff_at="2026-07-02T00:00:00+00:00",
        evidence_phase="pre_event",
        item_sha="a" * 64,
        response_sha="b" * 64,
        headline="Example Systems reports quarterly cloud results",
        url="https://example.com/demo-cloud",
            published_at="2026-07-01T21:30:00+00:00",
            retrieved_at="2026-07-02T01:00:00+00:00",
        source_name="Example News",
        source_domain="example.com",
        category="Earnings",
        related_tickers=("qqq",),
        relevance_basis=NewsSignalBasis.PROVIDER_NATIVE,
        sentiment_basis=NewsSignalBasis.PROVIDER_NATIVE,
    )

    assert receipt.ticker == "DEMO"
    assert receipt.related_tickers == ("QQQ",)
    assert receipt.rights_status == "provider_terms_review_pending"
    assert "summary" in receipt.excluded_text_fields
    assert permission_for(
        receipt, AnalyticalUse.DESCRIPTIVE_CONTEXT
    ) is AnalyticalPermission.ALLOWED
    assert permission_for(
        receipt, AnalyticalUse.SURPRISE_COMPUTATION
    ) is AnalyticalPermission.PROHIBITED


def test_news_evidence_receipt_rejects_text_smuggling_fields() -> None:
    payload = {
        "id": "ev_news_demo_1",
        "evidence_class": EvidenceClass.RECONSTRUCTED,
        "claim": "Provider metadata only.",
        "source_ref": "news:finnhub:finnhub-news-1",
        "provider_name": "finnhub",
        "provider_article_id": "finnhub-news-1",
        "ticker": "DEMO",
        "query_start": "2026-07-01",
            "query_end": "2026-07-02",
            "cutoff_at": "2026-07-02T00:00:00+00:00",
        "evidence_phase": "pre_event",
        "item_sha": "a" * 64,
        "response_sha": "b" * 64,
        "headline": "Example Systems reports quarterly results",
        "url": "https://example.com/demo",
            "published_at": "2026-07-01T21:30:00+00:00",
            "retrieved_at": "2026-07-02T01:00:00+00:00",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NewsEvidenceReceipt.model_validate(
            {**payload, "summary": "A provider-supplied article summary."}
        )
    with pytest.raises(ValidationError, match="compact metadata"):
        NewsEvidenceReceipt.model_validate(
            {**payload, "headline": "Headline\nFull article paragraph smuggled here."}
        )
    with pytest.raises(ValidationError, match="reconstructed metadata"):
        NewsEvidenceReceipt.model_validate(
            {**payload, "evidence_class": EvidenceClass.SOURCE_NATIVE}
        )
    with pytest.raises(ValidationError, match="query bounds"):
        NewsEvidenceReceipt.model_validate(
            {**payload, "published_at": "2026-07-03T00:00:00+00:00"}
        )
