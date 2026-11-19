from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AccountingBasis,
    AnalyticalUse,
    ClaimLocation,
    ClaimQuality,
    EvidenceClass,
    EvidenceReceipt,
    EvidenceRegistry,
    ExtractionRequest,
    ExtractionResult,
    FiscalPeriod,
    GrowthBasis,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
    SemanticClaim,
    build_extraction_prompt,
    extraction_json_schema,
    parse_extraction_output,
    validate_extraction,
)
from eventlens.persistence import SourcePassageRecord


PASSAGE_TEXT = "cloud and other cloud services revenue grew 39% year over year."


def _request(**changes: object) -> ExtractionRequest:
    passage = SourcePassageRecord(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        source_id=UUID("22222222-2222-2222-2222-222222222222"),
        passage_id="release:p17",
        ordinal=17,
        text=PASSAGE_TEXT,
        text_sha256=hashlib.sha256(PASSAGE_TEXT.encode("utf-8")).hexdigest(),
        char_start=500,
        char_end=500 + len(PASSAGE_TEXT),
        token_estimate=12,
        extraction_version="v1",
        created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    payload: dict[str, object] = {"passage": passage}
    payload.update(changes)
    return ExtractionRequest(**payload)


def _claim(request: ExtractionRequest, **changes: object) -> SemanticClaim:
    location = ClaimLocation(
        source_id=str(request.passage.source_id),
        passage_id=request.passage.passage_id,
        char_start=0,
        char_end=len(PASSAGE_TEXT),
    )
    proposition = "cloud and other cloud services revenue grew 39% year over year."
    receipt = PASSAGE_TEXT
    payload: dict[str, object] = {
        "id": "claim_cloud_growth",
        "proposition": proposition,
        "source_ref": request.source_ref,
        "location": location,
        "topics": ("cloud", "growth"),
        "quality": ClaimQuality.COMPLETE,
        "verbatim_receipt": receipt,
        "verbatim_receipt_sha256": hashlib.sha256(receipt.encode("utf-8")).hexdigest(),
        "evidence": EvidenceReceipt(
            id="claim_cloud_growth",
            evidence_class=EvidenceClass.SOURCE_NATIVE,
            claim=proposition,
            source_ref=request.source_ref,
        ),
        "metric": MetricValue(
            metric=NormalizedMetric.GROWTH,
            value=Decimal("39"),
            unit=MetricUnit.PERCENT,
            fiscal_period=FiscalPeriod(fiscal_year=2026, fiscal_quarter=2),
            accounting_basis=AccountingBasis.NOT_APPLICABLE,
            growth_basis=GrowthBasis.REPORTED,
            scope=MetricScope.PRODUCT,
        ),
    }
    payload.update(changes)
    return SemanticClaim(**payload)


def _result(request: ExtractionRequest, **changes: object) -> ExtractionResult:
    payload: dict[str, object] = {
        "source_ref": request.source_ref,
        "source_text_sha256": request.passage.text_sha256,
        "claims": (_claim(request),),
    }
    payload.update(changes)
    return ExtractionResult(**payload)


def test_accepts_a_complete_receipt_backed_cloud_growth_claim() -> None:
    request = _request()
    result = validate_extraction(_result(request), request)

    assert result.claims[0].metric is not None
    assert result.claims[0].metric.value == Decimal("39")


def test_model_output_is_parsed_directly_into_strict_typed_objects() -> None:
    request = _request()
    payload = _result(request).model_dump(mode="json")
    payload["unexpected"] = True

    with pytest.raises(ValidationError, match="unexpected"):
        parse_extraction_output(payload, request)

    with pytest.raises(ValidationError):
        parse_extraction_output("{not valid JSON", request)


def test_rejects_missing_or_unresolvable_receipt_pointer() -> None:
    request = _request()
    claim = _claim(request).model_copy(
        update={"source_ref": "source:wrong:passage:missing"}
    )
    # Pydantic catches mismatched internal pointers before source resolution.
    with pytest.raises(ValidationError, match="source_ref"):
        SemanticClaim.model_validate(claim.model_dump() | {"source_ref": "source:wrong:passage:missing"})

    incomplete = claim.model_dump()
    del incomplete["evidence"]
    with pytest.raises(ValidationError, match="evidence"):
        SemanticClaim.model_validate(incomplete)


def test_rejects_receipt_span_outside_source_passage() -> None:
    request = _request()
    claim = _claim(request).model_copy(
        update={
            "location": ClaimLocation(
                source_id=str(request.passage.source_id),
                passage_id=request.passage.passage_id,
                char_start=0,
                char_end=len(PASSAGE_TEXT) + 1,
            )
        }
    )
    with pytest.raises(ValueError, match="outside the source passage"):
        validate_extraction(_result(request, claims=(claim,)), request)


def test_rejects_semantically_truncated_proposition() -> None:
    request = _request()
    claim = _claim(request).model_copy(
        update={
            "proposition": "cloud and other cloud services revenue grew",
            "evidence": EvidenceReceipt(
                id="claim_cloud_growth",
                evidence_class=EvidenceClass.SOURCE_NATIVE,
                claim="cloud and other cloud services revenue grew",
                source_ref=request.source_ref,
            ),
        }
    )
    with pytest.raises(ValueError, match="semantically truncated"):
        validate_extraction(_result(request, claims=(claim,)), request)


def test_rejects_invalid_metric_unit_period_combination() -> None:
    request = _request()
    claim = _claim(request).model_copy(
        update={
            "metric": MetricValue(
                metric=NormalizedMetric.REVENUE,
                value=Decimal("39"),
                unit=MetricUnit.PERCENT,
                fiscal_period=FiscalPeriod(fiscal_year=2026, fiscal_quarter=2),
                accounting_basis=AccountingBasis.GAAP,
                growth_basis=GrowthBasis.NOT_APPLICABLE,
                scope=MetricScope.CONSOLIDATED,
            )
        }
    )
    with pytest.raises(ValueError, match="invalid unit"):
        validate_extraction(_result(request, claims=(claim,)), request)


def test_rejects_source_hash_mismatch_and_unauthorized_evidence_use() -> None:
    request = _request()
    with pytest.raises(ValueError, match="checksum"):
        validate_extraction(_result(request, source_text_sha256="0" * 64), request)

    causation_request = _request(analytical_use=AnalyticalUse.PROVE_CAUSATION)
    with pytest.raises(ValueError, match="not authorized"):
        validate_extraction(_result(causation_request), causation_request)


def test_semantic_claims_are_canonical_registry_entries_with_bounded_receipts() -> None:
    request = _request()
    claim = _claim(request)
    registry = EvidenceRegistry(
        receipts=(claim.evidence,), semantic_claims=(claim,)
    )

    assert registry.semantic_claims == (claim,)
    serialized = registry.model_dump(mode="json")
    assert serialized["semantic_claims"][0]["verbatim_receipt"] == PASSAGE_TEXT
    assert "text" not in serialized["semantic_claims"][0]

    with pytest.raises(ValueError, match="unresolved evidence receipt"):
        EvidenceRegistry(semantic_claims=(claim,))


def test_prompt_schema_is_strict_and_uses_only_the_requested_passage() -> None:
    request = _request()
    prompt = build_extraction_prompt(request)

    assert request.source_ref in prompt
    assert PASSAGE_TEXT in prompt
    assert extraction_json_schema()["additionalProperties"] is False
