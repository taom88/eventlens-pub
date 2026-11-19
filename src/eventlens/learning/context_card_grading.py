"""Deterministic evaluation of v4 context-card extraction and retrieval.

Fixtures contain labels and compact candidate metadata only; this module never
fetches sources, calls a model, or treats an evaluation result as a decision.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RetrievalMetrics(_EvalModel):
    k: int = Field(default=10, ge=1)
    recall_at_k: Decimal = Field(ge=0, le=1)
    precision_at_k: Decimal = Field(ge=0, le=1)
    tier_1_coverage: Decimal = Field(ge=0, le=1)
    tier_2_coverage: Decimal = Field(ge=0, le=1)
    duplicate_rate: Decimal = Field(ge=0, le=1)
    boilerplate_rate: Decimal = Field(ge=0, le=1)
    truncation_rate: Decimal = Field(ge=0, le=1)
    speaker_error_rate: Decimal = Field(ge=0, le=1)
    failures: tuple[str, ...] = ()


class ExtractionMetrics(_EvalModel):
    metric_identification: Decimal = Field(ge=0, le=1)
    value: Decimal = Field(ge=0, le=1)
    unit: Decimal = Field(ge=0, le=1)
    period: Decimal = Field(ge=0, le=1)
    basis: Decimal = Field(ge=0, le=1)
    speaker: Decimal = Field(ge=0, le=1)
    guidance_vs_actual: Decimal = Field(ge=0, le=1)
    exact_field_failures: tuple[str, ...] = ()


class AgentInterpretationCase(_EvalModel):
    """An adversarial semantic question and its expected safe interpretation."""

    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected: dict[str, Any]
    observed: dict[str, Any] = {}


class ContextCardEvalCase(_EvalModel):
    fixture_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    gold_fields: dict[str, Any] = {}
    predicted_fields: dict[str, Any] = {}
    ranked_candidates: tuple[dict[str, Any], ...] = ()
    expected_claim_ids: tuple[str, ...] = ()
    tier_1_claim_ids: tuple[str, ...] = ()
    tier_2_claim_ids: tuple[str, ...] = ()
    interpretation_cases: tuple[AgentInterpretationCase, ...] = ()
    extraction: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_fixture_id_alias(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "fixture_id" not in value and "id" in value:
            value = dict(value)
            value["fixture_id"] = value.pop("id")
        return value


class ContextCardEvalResult(_EvalModel):
    fixture_id: str = Field(min_length=1)
    retrieval: RetrievalMetrics
    extraction: ExtractionMetrics
    interpretation_passed: bool
    interpretation_failures: tuple[str, ...] = ()
    passed: bool
    failures: tuple[str, ...] = ()


def _rate(numerator: int, denominator: int) -> Decimal:
    return Decimal("0") if denominator == 0 else Decimal(numerator) / Decimal(denominator)


def _field_accuracy(gold: Mapping[str, Any], predicted: Mapping[str, Any], fields: tuple[str, ...]) -> tuple[Decimal, tuple[str, ...]]:
    failures: list[str] = []
    matched = 0
    for field in fields:
        if field in gold and gold.get(field) == predicted.get(field):
            matched += 1
        elif field in gold:
            failures.append(field)
    return _rate(matched, len([f for f in fields if f in gold])), tuple(failures)


def grade_extraction(case: ContextCardEvalCase) -> ExtractionMetrics:
    gold, predicted = case.gold_fields, case.predicted_fields
    groups = {
        "metric_identification": ("metric",), "value": ("value",),
        "unit": ("unit",), "period": ("period",), "basis": ("basis",),
        "speaker": ("speaker",), "guidance_vs_actual": ("claim_type",),
    }
    scores: dict[str, Decimal] = {}
    failures: list[str] = []
    for name, fields in groups.items():
        score, failed = _field_accuracy(gold, predicted, fields)
        scores[name] = score
        failures.extend(f"{case.fixture_id}.{field}" for field in failed)
    return ExtractionMetrics(**scores, exact_field_failures=tuple(failures))


def grade_retrieval(case: ContextCardEvalCase, *, k: int = 10) -> RetrievalMetrics:
    # A fixture may focus on extraction/interpretation and omit a ranking
    # payload; its gold claims are then the deterministic ideal ranking.
    ranked = (case.ranked_candidates or tuple({"claim_id": item} for item in case.expected_claim_ids))[:k]
    ids = [str(item.get("claim_id", item.get("id", ""))) for item in ranked]
    expected = set(case.expected_claim_ids)
    found = set(ids) & expected
    tier1, tier2 = set(case.tier_1_claim_ids), set(case.tier_2_claim_ids)
    duplicate_count = len(ids) - len(set(ids))
    boilerplate_count = sum(bool(item.get("boilerplate", False)) for item in ranked)
    truncation_count = sum(bool(item.get("truncated", item.get("truncation", False))) for item in ranked)
    speaker_errors = sum(bool(item.get("unresolved_management_speaker", item.get("speaker_error", False))) for item in ranked)
    failures: list[str] = []
    if tier1 and not tier1.issubset(set(ids)):
        failures.extend(f"retrieval.missing_tier1.{claim}" for claim in sorted(tier1 - set(ids)))
    if tier2 and not tier2.issubset(set(ids)):
        failures.extend(f"retrieval.missing_tier2.{claim}" for claim in sorted(tier2 - set(ids)))
    return RetrievalMetrics(
        k=k, recall_at_k=_rate(len(found), len(expected)),
        precision_at_k=_rate(len(found), len(ranked)),
        tier_1_coverage=_rate(len(tier1 & set(ids)), len(tier1)),
        tier_2_coverage=_rate(len(tier2 & set(ids)), len(tier2)),
        duplicate_rate=_rate(duplicate_count, len(ranked)),
        boilerplate_rate=_rate(boilerplate_count, len(ranked)),
        truncation_rate=_rate(truncation_count, len(ranked)),
        speaker_error_rate=_rate(speaker_errors, len(ranked)),
        failures=tuple(failures),
    )


def grade_interpretations(case: ContextCardEvalCase) -> tuple[bool, tuple[str, ...]]:
    failures: list[str] = []
    for item in case.interpretation_cases:
        for field, expected in item.expected.items():
            if item.observed.get(field) != expected:
                failures.append(f"{case.fixture_id}.{item.case_id}.{field}")
    return not failures, tuple(failures)


def evaluate_case(case: ContextCardEvalCase, *, k: int = 10) -> ContextCardEvalResult:
    retrieval = grade_retrieval(case, k=k)
    extraction = grade_extraction(case)
    interpretation_passed, interpretation_failures = grade_interpretations(case)
    failures = retrieval.failures + extraction.exact_field_failures + interpretation_failures
    # Targets are deliberately explicit and checked here so callers cannot
    # accidentally report a passing suite with unsafe retrieval behavior.
    if retrieval.tier_1_coverage < 1:
        failures += (f"{case.fixture_id}.tier1_recall",)
    if retrieval.tier_2_coverage <= Decimal("0.95") and case.tier_2_claim_ids:
        failures += (f"{case.fixture_id}.tier2_coverage",)
    if retrieval.boilerplate_rate >= Decimal("0.05"):
        failures += (f"{case.fixture_id}.boilerplate_rate",)
    if retrieval.truncation_rate > 0:
        failures += (f"{case.fixture_id}.truncation_rate",)
    if retrieval.speaker_error_rate >= Decimal("0.01"):
        failures += (f"{case.fixture_id}.speaker_error_rate",)
    if retrieval.duplicate_rate >= Decimal("0.05"):
        failures += (f"{case.fixture_id}.duplicate_rate",)
    return ContextCardEvalResult(fixture_id=case.fixture_id, retrieval=retrieval,
        extraction=extraction, interpretation_passed=interpretation_passed,
        interpretation_failures=interpretation_failures, passed=not failures,
        failures=tuple(dict.fromkeys(failures)))


def load_context_card_fixture(path: str | Path) -> ContextCardEvalCase:
    return ContextCardEvalCase.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def run_context_card_evaluations(cases: Iterable[ContextCardEvalCase | str | Path] | None = None, *, k: int = 10) -> tuple[ContextCardEvalResult, ...]:
    """Run deterministic evaluations for supplied cases or packaged fixtures."""
    if cases is None:
        root = Path(__file__).resolve().parents[3] / "fixtures" / "evals" / "context_cards"
        cases = sorted(root.glob("*.json"))
    loaded = (load_context_card_fixture(item) if isinstance(item, (str, Path)) else item for item in cases)
    return tuple(evaluate_case(case, k=k) for case in loaded)


__all__ = ["AgentInterpretationCase", "ContextCardEvalCase", "ContextCardEvalResult", "ExtractionMetrics", "RetrievalMetrics", "evaluate_case", "grade_extraction", "grade_interpretations", "grade_retrieval", "load_context_card_fixture", "run_context_card_evaluations"]
