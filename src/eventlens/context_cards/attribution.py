"""Typed, evidence-backed inference about factual market reactions.

Attribution is deliberately a weaker semantic layer than a measured return.
Nothing in this module calculates a return, fetches a source, or converts a
candidate explanation into source-native causation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .evidence import AnalyticalUse, EvidenceClass, EvidenceReceipt, require_permission
from .extraction import SemanticClaim

if TYPE_CHECKING:
    from .reactions import MarketReaction


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _AttributionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class AttributionConfidence(StrEnum):
    """Calibrated ordinal confidence in an inferred interpretation."""

    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AttributionWindow(StrEnum):
    """Reaction horizons whose information sets have different uncertainty."""

    AFTER_HOURS = "after_hours"
    T_PLUS_1 = "t_plus_1"
    T_PLUS_2 = "t_plus_2"
    T_PLUS_5 = "t_plus_5"


class AttributionDirection(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"


class AttributionImportance(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HumanReviewReason(StrEnum):
    MATERIAL_DOMINANT_DRIVER = "material_dominant_driver"
    SOURCE_ACCESS_LIMITATIONS = "source_access_limitations"


class AttributionCandidate(_AttributionModel):
    """One possible driver, traceable to both claims and their receipts."""

    id: NonEmptyStr
    factor: NonEmptyStr
    direction: AttributionDirection
    importance: AttributionImportance
    rationale: NonEmptyStr
    evidence_class: Literal[
        EvidenceClass.INFERRED, EvidenceClass.ATTRIBUTED
    ] = EvidenceClass.INFERRED
    semantic_claim_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    evidence_receipt_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def remains_an_inference(self) -> "AttributionCandidate":
        if self.evidence_class not in {EvidenceClass.INFERRED, EvidenceClass.ATTRIBUTED}:
            raise ValueError("attribution candidate evidence_class must be inferred or attributed")
        if len(set(self.semantic_claim_ids)) != len(self.semantic_claim_ids):
            raise ValueError("attribution candidate semantic claim ids must be unique")
        if len(set(self.evidence_receipt_ids)) != len(self.evidence_receipt_ids):
            raise ValueError("attribution candidate evidence receipt ids must be unique")
        if re.search(
            r"\b(?:stock|shares?|price|market reaction|move)\s+"
            r"(?:rose|fell|declined|gained|moved)\s+because\b"
            r"|\bcaused\s+(?:the\s+)?(?:stock|shares?|price|market reaction|move)\b",
            self.rationale,
            flags=re.IGNORECASE,
        ):
            raise ValueError("attribution rationale cannot state deterministic market causation")
        return self


class ReactionAttribution(_AttributionModel):
    """A bounded likely-driver interpretation for exactly one reaction window."""

    classification: Literal[EvidenceClass.INFERRED] = EvidenceClass.INFERRED
    window: AttributionWindow
    candidate_drivers: tuple[AttributionCandidate, ...] = Field(min_length=1)
    dominant_driver_id: NonEmptyStr | None = None
    dominant_driver_language: Literal["likely_reaction_driver"] = "likely_reaction_driver"
    confidence: AttributionConfidence
    confidence_rationale: NonEmptyStr
    confidence_adjustment_basis: NonEmptyStr | None = None
    alternative_explanations: tuple[NonEmptyStr, ...] = ()
    source_access_limitations: tuple[NonEmptyStr, ...] = ()
    material: bool = False
    requires_human_review: bool = False
    human_review_reasons: tuple[HumanReviewReason, ...] = ()

    @model_validator(mode="after")
    def attribution_is_cautious_and_reviewable(self) -> "ReactionAttribution":
        if self.classification is not EvidenceClass.INFERRED:
            raise ValueError("reaction attribution classification must be inferred")
        if self.dominant_driver_language != "likely_reaction_driver":
            raise ValueError("dominant driver language must be likely_reaction_driver")

        ids = tuple(candidate.id for candidate in self.candidate_drivers)
        if len(set(ids)) != len(ids):
            raise ValueError("attribution candidate ids must be unique")
        if len({candidate.factor for candidate in self.candidate_drivers}) != len(
            self.candidate_drivers
        ):
            raise ValueError("attribution candidate factors must be unique")

        if self.dominant_driver_id is not None:
            if self.dominant_driver_id not in ids:
                raise ValueError("dominant driver must identify a candidate driver")
            if len(self.candidate_drivers) < 2 or not self.alternative_explanations:
                raise ValueError("dominant driver requires candidate and narrative alternatives")
            if self.confidence is AttributionConfidence.UNKNOWN:
                raise ValueError("dominant driver requires calibrated attribution confidence")

        reasons = set(self.human_review_reasons)
        required_reasons: set[HumanReviewReason] = set()
        if self.material and self.dominant_driver_id is not None:
            required_reasons.add(HumanReviewReason.MATERIAL_DOMINANT_DRIVER)
        if self.source_access_limitations:
            required_reasons.add(HumanReviewReason.SOURCE_ACCESS_LIMITATIONS)
        if required_reasons and not self.requires_human_review:
            raise ValueError("material dominance or source limitations require human review")
        if not required_reasons.issubset(reasons):
            raise ValueError("human_review_reasons must identify every required review condition")
        if reasons and not self.requires_human_review:
            raise ValueError("human review reasons require requires_human_review=true")
        return self


@runtime_checkable
class AttributionAgent(Protocol):
    """Provider-free boundary for a schema-constrained attribution agent."""

    def attribute(
        self,
        reaction: MarketReaction,
        claims: Sequence[SemanticClaim],
        *,
        window: AttributionWindow,
    ) -> ReactionAttribution: ...


def validate_attribution(
    attribution: ReactionAttribution,
    semantic_claims: Iterable[SemanticClaim],
    evidence_receipts: Iterable[EvidenceReceipt],
) -> ReactionAttribution:
    """Resolve every candidate pointer to a canonical claim and receipt.

    A candidate may be inferred from source-native claims, but the candidate's
    own class must remain ``inferred`` or ``attributed``.  Claim/receipt pairs
    must match; listing two unrelated identifiers is not sufficient.
    """

    if not isinstance(attribution, ReactionAttribution):
        attribution = ReactionAttribution.model_validate(attribution)
    claims_by_id = _unique_by_id(semantic_claims, label="semantic claim")
    receipts_by_id = _unique_by_id(evidence_receipts, label="evidence receipt")
    registry = _ReceiptRegistry(receipts_by_id)

    for candidate in attribution.candidate_drivers:
        missing_claims = set(candidate.semantic_claim_ids) - claims_by_id.keys()
        if missing_claims:
            raise ValueError(
                f"attribution candidate {candidate.id!r} has unresolved semantic claims: "
                + ", ".join(sorted(missing_claims))
            )
        missing_receipts = set(candidate.evidence_receipt_ids) - receipts_by_id.keys()
        if missing_receipts:
            raise ValueError(
                f"attribution candidate {candidate.id!r} has unresolved evidence receipts: "
                + ", ".join(sorted(missing_receipts))
            )
        for claim_id in candidate.semantic_claim_ids:
            claim = claims_by_id[claim_id]
            if claim.evidence.id not in candidate.evidence_receipt_ids:
                raise ValueError(
                    f"attribution candidate {candidate.id!r} does not include the receipt "
                    f"for semantic claim {claim_id!r}"
                )
            if receipts_by_id[claim.evidence.id] != claim.evidence:
                raise ValueError(
                    f"semantic claim {claim_id!r} does not match its canonical receipt"
                )
        linked_receipts = {
            claims_by_id[claim_id].evidence.id for claim_id in candidate.semantic_claim_ids
        }
        if linked_receipts != set(candidate.evidence_receipt_ids):
            raise ValueError(
                f"attribution candidate {candidate.id!r} contains receipts not linked "
                "to its semantic claims"
            )
        for receipt_id in candidate.evidence_receipt_ids:
            try:
                require_permission(
                    receipts_by_id[receipt_id],
                    AnalyticalUse.SUPPORT_INTERPRETATION,
                    registry=registry,
                )
            except PermissionError as exc:
                raise ValueError(
                    f"attribution candidate {candidate.id!r} uses evidence that is not "
                    "permitted for interpretation"
                ) from exc
    return attribution


def decay_attribution_confidence(
    confidence: AttributionConfidence,
    *,
    from_window: AttributionWindow,
    to_window: AttributionWindow,
    explicit_adjustment_basis: str | None = None,
) -> AttributionConfidence:
    """Return the confidence ceiling at a later reaction horizon.

    Moving from an immediate window to uncontrolled T+5 loses one calibrated
    level.  An explicit benchmark/event adjustment basis preserves (but never
    increases) the input confidence.  Low and unknown are stable floors.
    """

    confidence = AttributionConfidence(confidence)
    from_window = AttributionWindow(from_window)
    to_window = AttributionWindow(to_window)
    order = {
        AttributionWindow.AFTER_HOURS: 0,
        AttributionWindow.T_PLUS_1: 1,
        AttributionWindow.T_PLUS_2: 2,
        AttributionWindow.T_PLUS_5: 5,
    }
    if order[to_window] < order[from_window]:
        raise ValueError("confidence decay cannot move to an earlier reaction window")
    if explicit_adjustment_basis is not None and not explicit_adjustment_basis.strip():
        raise ValueError("explicit adjustment basis must not be blank")
    if (
        to_window is not AttributionWindow.T_PLUS_5
        or from_window is AttributionWindow.T_PLUS_5
        or explicit_adjustment_basis is not None
    ):
        return confidence
    return {
        AttributionConfidence.HIGH: AttributionConfidence.MEDIUM,
        AttributionConfidence.MEDIUM: AttributionConfidence.LOW,
        AttributionConfidence.LOW: AttributionConfidence.LOW,
        AttributionConfidence.UNKNOWN: AttributionConfidence.UNKNOWN,
    }[confidence]


def validate_attribution_horizons(
    attributions: Iterable[ReactionAttribution],
) -> tuple[ReactionAttribution, ...]:
    """Enforce uniqueness and the uncontrolled T+5 confidence ceiling."""

    values = tuple(attributions)
    by_window: dict[AttributionWindow, ReactionAttribution] = {}
    for attribution in values:
        if attribution.window in by_window:
            raise ValueError("only one attribution per reaction window is allowed")
        by_window[attribution.window] = attribution

    t_plus_5 = by_window.get(AttributionWindow.T_PLUS_5)
    if t_plus_5 is None or t_plus_5.confidence_adjustment_basis is not None:
        return values
    prior = next(
        (
            by_window[window]
            for window in (
                AttributionWindow.T_PLUS_2,
                AttributionWindow.T_PLUS_1,
                AttributionWindow.AFTER_HOURS,
            )
            if window in by_window
        ),
        None,
    )
    if prior is None:
        return values
    ceiling = decay_attribution_confidence(
        prior.confidence,
        from_window=prior.window,
        to_window=AttributionWindow.T_PLUS_5,
    )
    ranks = {
        AttributionConfidence.UNKNOWN: 0,
        AttributionConfidence.LOW: 1,
        AttributionConfidence.MEDIUM: 2,
        AttributionConfidence.HIGH: 3,
    }
    if ranks[t_plus_5.confidence] > ranks[ceiling]:
        raise ValueError(
            "uncontrolled T+5 attribution confidence must decay below the shorter window"
        )
    return values


_ItemWithId = TypeVar("_ItemWithId", SemanticClaim, EvidenceReceipt)


class _ReceiptRegistry:
    """Minimal policy-compatible view without importing the card aggregate."""

    def __init__(self, receipts_by_id: Mapping[str, EvidenceReceipt]) -> None:
        self.receipts_by_id = receipts_by_id


def _unique_by_id(
    items: Iterable[_ItemWithId], *, label: str
) -> Mapping[str, _ItemWithId]:
    by_id: dict[str, _ItemWithId] = {}
    for item in items:
        item_id = getattr(item, "id", None)
        if not isinstance(item_id, str) or not item_id:
            raise TypeError(f"{label} objects must expose a non-empty id")
        if item_id in by_id:
            raise ValueError(f"{label} ids must be unique")
        by_id[item_id] = item
    return by_id


__all__ = [
    "AttributionAgent",
    "AttributionCandidate",
    "AttributionConfidence",
    "AttributionDirection",
    "AttributionImportance",
    "AttributionWindow",
    "HumanReviewReason",
    "ReactionAttribution",
    "decay_attribution_confidence",
    "validate_attribution",
    "validate_attribution_horizons",
]
