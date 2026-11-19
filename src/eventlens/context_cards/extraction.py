"""Schema-constrained semantic extraction from immutable source passages.

This module is deliberately provider-free.  An extractor may be backed by a
model elsewhere, but its untrusted output must parse as :class:`ExtractionResult`
and pass :func:`validate_extraction` before it can be used by a card.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Protocol, TypeAlias, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from eventlens.persistence import SourcePassageRecord

from .evidence import (
    DEFAULT_EVIDENCE_POLICY,
    AnalyticalUse,
    EvidencePolicy,
    EvidenceReceipt,
    require_permission,
)
from .metrics import MetricUnit, MetricValue, NormalizedMetric


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
JsonObject: TypeAlias = Mapping[str, Any]


class _ExtractionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True
    )


class ClaimQuality(StrEnum):
    """Completeness assessment of a claim, never a truth assessment."""

    COMPLETE = "complete"
    AMBIGUOUS = "ambiguous"
    PARTIAL = "partial"


class ClaimLocation(_ExtractionModel):
    """A passage-relative, half-open character span and its stable pointers."""

    source_id: NonEmptyStr
    passage_id: NonEmptyStr
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)

    @model_validator(mode="after")
    def span_is_nonempty(self) -> "ClaimLocation":
        if self.char_end <= self.char_start:
            raise ValueError("claim location char_end must follow char_start")
        return self

    @property
    def source_ref(self) -> str:
        """Canonical receipt pointer for the immutable source passage."""

        return f"source:{self.source_id}:passage:{self.passage_id}"


class SemanticClaim(_ExtractionModel):
    """One coherent proposition with a bounded, exact source receipt."""

    id: NonEmptyStr
    proposition: NonEmptyStr
    source_ref: NonEmptyStr
    location: ClaimLocation
    topics: tuple[NonEmptyStr, ...] = Field(min_length=1)
    quality: ClaimQuality
    verbatim_receipt: NonEmptyStr
    verbatim_receipt_sha256: Sha256
    evidence: EvidenceReceipt
    metric: MetricValue | None = None

    @model_validator(mode="after")
    def self_describes_a_resolvable_receipt(self) -> "SemanticClaim":
        if len(set(self.topics)) != len(self.topics):
            raise ValueError("claim topics cannot contain duplicates")
        if self.source_ref != self.location.source_ref:
            raise ValueError("claim source_ref must match its location pointer")
        if self.evidence.id != self.id:
            raise ValueError("claim evidence id must match claim id")
        if self.evidence.source_ref != self.source_ref:
            raise ValueError("claim evidence source_ref must match claim source_ref")
        if self.evidence.claim != self.proposition:
            raise ValueError("claim evidence claim must exactly match proposition")
        if hashlib.sha256(self.verbatim_receipt.encode("utf-8")).hexdigest() != self.verbatim_receipt_sha256:
            raise ValueError("verbatim receipt checksum does not match receipt text")
        if self.metric is not None and self.metric.value is None:
            raise ValueError("an extracted metric must carry an explicit value")
        return self


class ExtractionRequest(_ExtractionModel):
    """The bounded source input and permitted analytical use for extraction."""

    passage: SourcePassageRecord
    analytical_use: AnalyticalUse = AnalyticalUse.DESCRIPTIVE_CONTEXT

    @property
    def source_ref(self) -> str:
        return f"source:{self.passage.source_id}:passage:{self.passage.passage_id}"


class ExtractionResult(_ExtractionModel):
    """Model output envelope; it intentionally excludes the source body."""

    source_ref: NonEmptyStr
    source_text_sha256: Sha256
    claims: tuple[SemanticClaim, ...] = ()

    @model_validator(mode="after")
    def claims_are_distinct(self) -> "ExtractionResult":
        claim_ids = tuple(claim.id for claim in self.claims)
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("extraction result claim ids must be unique")
        return self


@runtime_checkable
class SemanticClaimExtractor(Protocol):
    """A provider boundary.  Live provider integration is intentionally absent."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult: ...


def parse_extraction_output(
    output: str | bytes | JsonObject,
    request: ExtractionRequest,
    *,
    policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
) -> ExtractionResult:
    """Parse untrusted structured output and validate it against ``request``."""

    if isinstance(output, (str, bytes, bytearray)):
        result = ExtractionResult.model_validate_json(output)
    else:
        result = ExtractionResult.model_validate(output)
    return validate_extraction(result, request, policy=policy)


def validate_extraction(
    result: ExtractionResult | JsonObject,
    request: ExtractionRequest,
    *,
    policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
) -> ExtractionResult:
    """Fail closed unless every claim is exact, complete, and policy-permitted.

    Locations use passage-relative offsets.  The only source text permitted in
    a result is the exact bounded ``verbatim_receipt`` for an individual claim.
    """

    parsed = result if isinstance(result, ExtractionResult) else ExtractionResult.model_validate(result)
    passage = request.passage
    if parsed.source_ref != request.source_ref:
        raise ValueError("extraction result source_ref does not resolve to request passage")
    if parsed.source_text_sha256 != passage.text_sha256:
        raise ValueError("extraction result source text checksum does not match request passage")

    for claim in parsed.claims:
        _validate_claim(claim, request=request, policy=policy)
    return parsed


def _validate_claim(
    claim: SemanticClaim,
    *,
    request: ExtractionRequest,
    policy: EvidencePolicy | None,
) -> None:
    passage = request.passage
    if claim.location.source_id != str(passage.source_id) or claim.location.passage_id != passage.passage_id:
        raise ValueError("claim location does not resolve to request passage")
    if claim.location.char_end > len(passage.text):
        raise ValueError("claim receipt span is outside the source passage")
    receipt = passage.text[claim.location.char_start : claim.location.char_end]
    if receipt != claim.verbatim_receipt:
        raise ValueError("claim verbatim receipt must exactly match its bounded source span")
    _assert_complete_proposition(claim)
    if claim.metric is not None:
        _validate_metric_semantics(claim.metric)
    try:
        require_permission(claim.evidence, request.analytical_use, policy=policy)
    except PermissionError as exc:
        raise ValueError("claim evidence is not authorized for the requested analytical use") from exc


_DANGLING_WORD = re.compile(r"(?:and|or|but|because|of|to|for|with|versus|vs)\s*[,:;]?$", re.IGNORECASE)


def _assert_complete_proposition(claim: SemanticClaim) -> None:
    """Reject mechanically detectable fragments before downstream interpretation.

    Semantic completeness cannot be inferred perfectly without a reviewer, but
    a complete-quality claim must be a sentence-like proposition and its
    receipt must not visibly end at an unfinished connective or punctuation.
    """

    if claim.quality is not ClaimQuality.COMPLETE:
        raise ValueError("only complete semantic claims may enter the extraction result")
    words = re.findall(r"[\w%]+", claim.proposition)
    if len(words) < 3 or not claim.proposition.rstrip().endswith((".", "!", "?")):
        raise ValueError("claim proposition is semantically truncated")
    receipt = claim.verbatim_receipt.rstrip()
    if not receipt.endswith((".", "!", "?")) or _DANGLING_WORD.search(receipt):
        raise ValueError("claim receipt is semantically truncated")


def _validate_metric_semantics(metric: MetricValue) -> None:
    """Enforce metric/unit pairings beyond the generic metric normalizer."""

    money_metrics = {
        NormalizedMetric.REVENUE,
        NormalizedMetric.GROSS_PROFIT,
        NormalizedMetric.OPERATING_INCOME,
        NormalizedMetric.NET_INCOME,
        NormalizedMetric.FREE_CASH_FLOW,
    }
    if metric.metric in money_metrics and metric.unit not in {
        MetricUnit.USD,
        MetricUnit.USD_MILLION,
        MetricUnit.USD_BILLION,
    }:
        raise ValueError("monetary metric has an invalid unit")
    if metric.metric in {NormalizedMetric.EPS, NormalizedMetric.DILUTED_EPS, NormalizedMetric.ADJUSTED_EPS} and metric.unit is not MetricUnit.USD_PER_SHARE:
        raise ValueError("EPS metric requires USD_per_share unit")
    if metric.metric is NormalizedMetric.OPERATING_MARGIN and metric.unit not in {
        MetricUnit.PERCENT,
        MetricUnit.RATIO,
        MetricUnit.PERCENTAGE_POINTS,
    }:
        raise ValueError("operating margin has an invalid unit")
    if metric.metric is NormalizedMetric.SHARE_COUNT and metric.unit is not MetricUnit.COUNT:
        raise ValueError("share count requires count unit")


__all__ = [
    "ClaimLocation",
    "ClaimQuality",
    "ExtractionRequest",
    "ExtractionResult",
    "SemanticClaim",
    "SemanticClaimExtractor",
    "parse_extraction_output",
    "validate_extraction",
]
