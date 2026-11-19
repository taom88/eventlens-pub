"""Need-first, ontology-keyed retrieval for v4 context cards.

This module ranks already-extracted semantic claims. It does not search raw
documents, call providers, or use embeddings. Every scoring input and penalty
is explicit so a reviewer can reproduce the ranking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .evidence import (
    DEFAULT_EVIDENCE_POLICY,
    AnalyticalPermission,
    AnalyticalUse,
    EvidencePolicy,
    permission_for,
)
from .extraction import SemanticClaim
from .metrics import FiscalPeriod
from .ontology import KpiOntology


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
UnitInterval = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class _RetrievalModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, validate_default=True
    )


class InformationNeed(_RetrievalModel):
    """One retrieval job for one canonical issuer-ontology slot."""

    ontology_slot: NonEmptyStr
    description: NonEmptyStr
    analytical_use: AnalyticalUse = AnalyticalUse.DESCRIPTIVE_CONTEXT
    current_state: bool = False
    max_results: int = Field(default=3, ge=1)

    @property
    def id(self) -> str:
        return self.ontology_slot


class RetrievalCandidate(_RetrievalModel):
    """A verified claim plus explicit normalized ranking inputs.

    Dimensions and penalties use ``[0, 1]``. One is maximum strength for a
    positive dimension or maximum severity for a penalty.
    """

    claim: SemanticClaim
    ontology_slots: tuple[NonEmptyStr, ...] = Field(min_length=1)
    fiscal_period: FiscalPeriod | None = None
    normalized_claim_key: NonEmptyStr | None = None
    conflict_evidence: bool = False

    source_authority: UnitInterval
    semantic_relevance: UnitInterval
    decision_relevance: UnitInterval
    temporal_relevance: UnitInterval
    numeric_specificity: UnitInterval
    forward_information_value: UnitInterval
    novelty: UnitInterval

    boilerplate: UnitInterval = Decimal("0")
    metric_definition_only: UnitInterval = Decimal("0")
    truncation: UnitInterval = Decimal("0")
    duplicate_information: UnitInterval = Decimal("0")
    low_thesis_relevance: UnitInterval = Decimal("0")
    unresolved_speaker: UnitInterval = Decimal("0")

    @model_validator(mode="after")
    def candidate_is_coherent(self) -> "RetrievalCandidate":
        if len(set(self.ontology_slots)) != len(self.ontology_slots):
            raise ValueError("candidate ontology slots must be unique")
        metric_period = self.claim.metric.fiscal_period if self.claim.metric is not None else None
        if self.fiscal_period is not None and metric_period is not None and self.fiscal_period != metric_period:
            raise ValueError("candidate fiscal period must match its normalized metric")
        return self

    @property
    def id(self) -> str:
        return self.claim.id

    @property
    def effective_fiscal_period(self) -> FiscalPeriod | None:
        if self.fiscal_period is not None:
            return self.fiscal_period
        return self.claim.metric.fiscal_period if self.claim.metric is not None else None

    @property
    def diversity_key(self) -> str:
        """Return a deterministic normalized-fact key for receipt capping."""

        if self.normalized_claim_key is not None:
            return self.normalized_claim_key.casefold()
        metric = self.claim.metric
        if metric is not None:
            return "|".join(
                (
                    metric.metric.value,
                    str(metric.value),
                    metric.unit.value,
                    metric.fiscal_period.label,
                    metric.accounting_basis.value,
                    metric.growth_basis.value,
                    metric.scope.value,
                )
            )
        return " ".join(re.findall(r"[a-z0-9]+", self.claim.proposition.casefold()))


class RetrievalScore(_RetrievalModel):
    """Auditable score breakdown for one candidate against one need."""

    ontology_slot: NonEmptyStr
    candidate_id: NonEmptyStr
    eligible: bool
    rejection_reason: NonEmptyStr | None = None

    source_authority: UnitInterval
    semantic_relevance: UnitInterval
    decision_relevance: UnitInterval
    temporal_relevance: UnitInterval
    numeric_specificity: UnitInterval
    forward_information_value: UnitInterval
    novelty: UnitInterval

    boilerplate: NonNegative
    metric_definition_only: NonNegative
    truncation: NonNegative
    duplicate_information: NonNegative
    low_thesis_relevance: NonNegative
    unresolved_speaker: NonNegative
    total: UnitInterval

    @model_validator(mode="after")
    def eligibility_is_explained(self) -> "RetrievalScore":
        if self.eligible == (self.rejection_reason is not None):
            raise ValueError("only an ineligible score may carry a rejection reason")
        if not self.eligible and self.total != 0:
            raise ValueError("an ineligible candidate must have a zero score")
        return self


class RetrievalPolicy(_RetrievalModel):
    """Versioned deterministic weights and diversity constraints."""

    version: NonEmptyStr = "1.0.0-proposed.1"

    source_authority_weight: NonNegative = Decimal("0.16")
    semantic_relevance_weight: NonNegative = Decimal("0.22")
    decision_relevance_weight: NonNegative = Decimal("0.14")
    temporal_relevance_weight: NonNegative = Decimal("0.14")
    numeric_specificity_weight: NonNegative = Decimal("0.10")
    forward_information_value_weight: NonNegative = Decimal("0.14")
    novelty_weight: NonNegative = Decimal("0.10")

    boilerplate_penalty: NonNegative = Decimal("0.30")
    metric_definition_only_penalty: NonNegative = Decimal("0.15")
    truncation_penalty: NonNegative = Decimal("1.00")
    duplicate_information_penalty: NonNegative = Decimal("0.20")
    low_thesis_relevance_penalty: NonNegative = Decimal("0.15")
    unresolved_speaker_penalty: NonNegative = Decimal("1.00")

    max_receipts_per_normalized_claim: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def positive_dimensions_have_weight(self) -> "RetrievalPolicy":
        if self.positive_weight_total <= 0:
            raise ValueError("at least one positive retrieval dimension must have weight")
        return self

    @property
    def positive_weight_total(self) -> Decimal:
        return sum(
            (
                self.source_authority_weight,
                self.semantic_relevance_weight,
                self.decision_relevance_weight,
                self.temporal_relevance_weight,
                self.numeric_specificity_weight,
                self.forward_information_value_weight,
                self.novelty_weight,
            ),
            Decimal("0"),
        )


DEFAULT_RETRIEVAL_POLICY = RetrievalPolicy()


def score_candidate(
    candidate: RetrievalCandidate,
    need: InformationNeed,
    *,
    policy: RetrievalPolicy = DEFAULT_RETRIEVAL_POLICY,
    evidence_policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
    evidence_registry: object | None = None,
) -> RetrievalScore:
    """Score a candidate with a fully visible, deterministic breakdown."""

    rejection_reason: str | None = None
    if need.ontology_slot not in candidate.ontology_slots:
        rejection_reason = "candidate does not address the requested ontology slot"
    elif permission_for(
        candidate.claim.evidence,
        need.analytical_use,
        policy=evidence_policy,
        registry=evidence_registry,
    ) is not AnalyticalPermission.ALLOWED:
        rejection_reason = "candidate evidence is not permitted for the requested analytical use"

    positive = sum(
        (
            candidate.source_authority * policy.source_authority_weight,
            candidate.semantic_relevance * policy.semantic_relevance_weight,
            candidate.decision_relevance * policy.decision_relevance_weight,
            candidate.temporal_relevance * policy.temporal_relevance_weight,
            candidate.numeric_specificity * policy.numeric_specificity_weight,
            candidate.forward_information_value * policy.forward_information_value_weight,
            candidate.novelty * policy.novelty_weight,
        ),
        Decimal("0"),
    ) / policy.positive_weight_total

    penalty_values = {
        "boilerplate": candidate.boilerplate * policy.boilerplate_penalty,
        "metric_definition_only": candidate.metric_definition_only * policy.metric_definition_only_penalty,
        "truncation": candidate.truncation * policy.truncation_penalty,
        "duplicate_information": candidate.duplicate_information * policy.duplicate_information_penalty,
        "low_thesis_relevance": candidate.low_thesis_relevance * policy.low_thesis_relevance_penalty,
        "unresolved_speaker": candidate.unresolved_speaker * policy.unresolved_speaker_penalty,
    }
    total = max(Decimal("0"), min(Decimal("1"), positive - sum(penalty_values.values())))
    if rejection_reason is not None:
        total = Decimal("0")

    return RetrievalScore(
        ontology_slot=need.ontology_slot,
        candidate_id=candidate.id,
        eligible=rejection_reason is None,
        rejection_reason=rejection_reason,
        source_authority=candidate.source_authority,
        semantic_relevance=candidate.semantic_relevance,
        decision_relevance=candidate.decision_relevance,
        temporal_relevance=candidate.temporal_relevance,
        numeric_specificity=candidate.numeric_specificity,
        forward_information_value=candidate.forward_information_value,
        novelty=candidate.novelty,
        **penalty_values,
        total=total,
    )


def select_diverse_claims(
    candidates: Iterable[RetrievalCandidate],
    need: InformationNeed,
    *,
    policy: RetrievalPolicy = DEFAULT_RETRIEVAL_POLICY,
    evidence_policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
    evidence_registry: object | None = None,
) -> tuple[RetrievalCandidate, ...]:
    """Select claims by marginal information gain with a duplicate cap."""

    remaining = list(candidates)
    selected: list[RetrievalCandidate] = []
    receipt_counts: dict[str, int] = {}
    if len({candidate.id for candidate in remaining}) != len(remaining):
        raise ValueError("retrieval candidates must have unique claim ids")

    while remaining and len(selected) < need.max_results:
        ranked: list[tuple[tuple[object, ...], RetrievalCandidate]] = []
        for candidate in remaining:
            count = receipt_counts.get(candidate.diversity_key, 0)
            if count >= policy.max_receipts_per_normalized_claim and not candidate.conflict_evidence:
                continue
            marginal = candidate.model_copy(
                update={
                    "novelty": candidate.novelty if count == 0 else Decimal("0"),
                    "duplicate_information": max(
                        candidate.duplicate_information,
                        Decimal("1") if count else Decimal("0"),
                    ),
                }
            )
            score = score_candidate(
                marginal,
                need,
                policy=policy,
                evidence_policy=evidence_policy,
                evidence_registry=evidence_registry,
            )
            if score.eligible:
                ranked.append((_rank_key(candidate, score, need), candidate))
        if not ranked:
            break
        _, chosen = min(ranked, key=lambda item: item[0])
        selected.append(chosen)
        receipt_counts[chosen.diversity_key] = receipt_counts.get(chosen.diversity_key, 0) + 1
        remaining.remove(chosen)
    return tuple(selected)


def retrieve_for_slots(
    needs: Iterable[InformationNeed],
    candidates: Iterable[RetrievalCandidate],
    *,
    ontology: KpiOntology,
    policy: RetrievalPolicy = DEFAULT_RETRIEVAL_POLICY,
    evidence_policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
    evidence_registry: object | None = None,
) -> dict[str, tuple[RetrievalCandidate, ...]]:
    """Retrieve independently within each requested ontology slot."""

    need_items = tuple(needs)
    candidate_items = tuple(candidates)
    need_slots = tuple(need.ontology_slot for need in need_items)
    if len(set(need_slots)) != len(need_slots):
        raise ValueError("information needs must have unique ontology slots")
    if len({candidate.id for candidate in candidate_items}) != len(candidate_items):
        raise ValueError("retrieval candidates must have unique claim ids")

    known_slots = ontology.kpis_by_id
    unknown_needs = sorted(set(need_slots) - set(known_slots))
    if unknown_needs:
        raise ValueError(f"information needs reference unknown ontology slots: {unknown_needs}")
    unknown_candidate_slots = sorted(
        {slot for candidate in candidate_items for slot in candidate.ontology_slots} - set(known_slots)
    )
    if unknown_candidate_slots:
        raise ValueError(
            f"retrieval candidates reference unknown ontology slots: {unknown_candidate_slots}"
        )

    return {
        need.ontology_slot: select_diverse_claims(
            (candidate for candidate in candidate_items if need.ontology_slot in candidate.ontology_slots),
            need,
            policy=policy,
            evidence_policy=evidence_policy,
            evidence_registry=evidence_registry,
        )
        for need in need_items
    }


def _rank_key(
    candidate: RetrievalCandidate,
    score: RetrievalScore,
    need: InformationNeed,
) -> tuple[object, ...]:
    period = candidate.effective_fiscal_period
    if need.current_state:
        # Period-first by contract: older evidence cannot beat the latest state
        # merely because its prose is more polished.
        period_key = (
            -(period.fiscal_year if period is not None else -1),
            -(period.fiscal_quarter or 0) if period is not None else 0,
        )
    else:
        period_key = (0, 0)
    return (
        *period_key,
        -score.total,
        candidate.claim.id,
        candidate.claim.source_ref,
        candidate.claim.location.char_start,
        candidate.claim.location.char_end,
    )


__all__ = [
    "DEFAULT_RETRIEVAL_POLICY",
    "InformationNeed",
    "RetrievalCandidate",
    "RetrievalPolicy",
    "RetrievalScore",
    "retrieve_for_slots",
    "score_candidate",
    "select_diverse_claims",
]
