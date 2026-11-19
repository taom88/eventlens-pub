"""Financial-context quality evaluation over immutable historical card artifacts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RUBRIC_VERSION = "eventlens.context-card-eval.v0.1"
DATASET_VERSION = "eventlens.context-card-quality-dataset.v0.1"
REPORT_VERSION = "eventlens.context-card-quality-report.v0.1"
COMPARISON_VERSION = "eventlens.context-card-quality-comparison.v0.1"


class _QualityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DimensionName(StrEnum):
    RELIABILITY = "factual_reliability"
    COMPLETENESS = "critical_information_completeness"
    RELEVANCE = "decision_relevance"
    USABILITY = "downstream_usability"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"


class EvaluationStatus(StrEnum):
    PASS = "PASS"
    BELOW_TARGET = "BELOW_TARGET"
    FAIL = "FAIL"
    NOT_SCORED = "NOT_SCORED"


class RubricDimension(_QualityModel):
    weight: Decimal = Field(gt=0, le=1)
    anchors: dict[int, str]

    @model_validator(mode="after")
    def anchors_cover_scale(self) -> RubricDimension:
        if set(self.anchors) != set(range(6)):
            raise ValueError(
                "dimension anchors must define every score from 0 through 5"
            )
        return self


class ContextCardRubric(_QualityModel):
    schema_version: Literal[RUBRIC_VERSION] = RUBRIC_VERSION
    target_score: Decimal = Field(ge=0, le=100)
    dimensions: dict[DimensionName, RubricDimension]
    critical_gates: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> ContextCardRubric:
        if set(self.dimensions) != set(DimensionName):
            raise ValueError("rubric must define all four context-quality dimensions")
        if sum(item.weight for item in self.dimensions.values()) != Decimal(1):
            raise ValueError("rubric weights must sum to one")
        return self


class ExpectedMetric(_QualityModel):
    metric: str = Field(min_length=1)
    value: Decimal
    unit: str = Field(min_length=1)
    accounting_basis: str = Field(min_length=1)


class DatasetCase(_QualityModel):
    case_id: str = Field(min_length=1)
    role: Literal["benchmark", "additional", "held_out"]
    ticker: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    fiscal_period: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_metrics: tuple[ExpectedMetric, ...] = Field(min_length=1)


class ContextCardDataset(_QualityModel):
    schema_version: Literal[DATASET_VERSION] = DATASET_VERSION
    dataset_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_input_path: str = Field(min_length=1)
    cases: tuple[DatasetCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def cases_are_unique_and_bounded(self) -> ContextCardDataset:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset case IDs must be unique")
        if sum(case.role == "benchmark" for case in self.cases) != 1:
            raise ValueError("dataset must contain exactly one benchmark case")
        return self


class ReviewScore(_QualityModel):
    score: Decimal = Field(ge=0, le=5)
    rationale: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class HumanReview(_QualityModel):
    schema_version: Literal[RUBRIC_VERSION] = RUBRIC_VERSION
    case_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1)
    reviewed_at: datetime
    dimensions: dict[DimensionName, ReviewScore]

    @model_validator(mode="after")
    def review_is_complete(self) -> HumanReview:
        if set(self.dimensions) != set(DimensionName):
            raise ValueError("human review must score all four dimensions")
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        return self


class AutomatedCheck(_QualityModel):
    check_id: str = Field(min_length=1)
    dimension: DimensionName
    status: CheckStatus
    critical: bool = False
    detail: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def critical_checks_are_deterministic(self) -> AutomatedCheck:
        if self.critical and self.status is CheckStatus.REVIEW:
            raise ValueError(
                "a review requirement cannot masquerade as a critical gate"
            )
        return self


class DimensionResult(_QualityModel):
    weight: Decimal
    automated_checks: tuple[AutomatedCheck, ...]
    review_score: Decimal | None = Field(default=None, ge=0, le=5)
    weighted_points: Decimal | None = Field(default=None, ge=0, le=100)
    review_rationale: str | None = None
    review_evidence_refs: tuple[str, ...] = ()


class ContextCardQualityResult(_QualityModel):
    schema_version: Literal[REPORT_VERSION] = REPORT_VERSION
    case_id: str
    role: Literal["benchmark", "additional", "held_out"]
    ticker: str
    event_id: str
    fiscal_period: str
    artifact_path: str
    artifact_sha256: str
    source_input_sha256: str
    rubric_version: Literal[RUBRIC_VERSION] = RUBRIC_VERSION
    rubric_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: EvaluationStatus
    score: Decimal | None = Field(default=None, ge=0, le=100)
    target_score: Decimal
    critical_failures: tuple[str, ...] = ()
    dimensions: dict[DimensionName, DimensionResult]
    missing_review_dimensions: tuple[DimensionName, ...] = ()


class ContextCardQualityReport(_QualityModel):
    schema_version: Literal[REPORT_VERSION] = REPORT_VERSION
    dataset_id: str
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rubric_version: Literal[RUBRIC_VERSION] = RUBRIC_VERSION
    rubric_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    results: tuple[ContextCardQualityResult, ...]
    status_counts: dict[EvaluationStatus, int]
    hard_failures: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    largest_bottleneck: str
    recommended_improvements: tuple[str, ...] = Field(min_length=1, max_length=3)
    coverage_limitations: tuple[str, ...]


class CaseComparison(_QualityModel):
    case_id: str
    before_artifact_sha256: str
    after_artifact_sha256: str
    score_delta: Decimal | None = None
    resolved_failures: tuple[str, ...] = ()
    new_failures: tuple[str, ...] = ()
    status_changed: bool


class ContextCardQualityComparison(_QualityModel):
    schema_version: Literal[COMPARISON_VERSION] = COMPARISON_VERSION
    dataset_id: str
    rubric_version: Literal[RUBRIC_VERSION] = RUBRIC_VERSION
    rubric_sha256: str
    cases: tuple[CaseComparison, ...]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rubric(path: str | Path) -> tuple[ContextCardRubric, str]:
    source = Path(path)
    return (
        ContextCardRubric.model_validate(
            json.loads(source.read_text(encoding="utf-8"))
        ),
        _sha256(source),
    )


def load_dataset(path: str | Path) -> tuple[ContextCardDataset, str]:
    source = Path(path)
    return (
        ContextCardDataset.model_validate(
            json.loads(source.read_text(encoding="utf-8"))
        ),
        _sha256(source),
    )


def load_review(path: str | Path) -> HumanReview:
    return HumanReview.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def _check(
    check_id: str,
    dimension: DimensionName,
    passed: bool | None,
    detail: str,
    *,
    critical: bool = False,
    refs: tuple[str, ...] = (),
) -> AutomatedCheck:
    status = (
        CheckStatus.REVIEW
        if passed is None
        else CheckStatus.PASS
        if passed
        else CheckStatus.FAIL
    )
    return AutomatedCheck(
        check_id=check_id,
        dimension=dimension,
        status=status,
        critical=critical,
        detail=detail,
        evidence_refs=refs,
    )


def _raw_checks(
    raw: dict[str, Any], case: DatasetCase, actual_sha256: str
) -> list[AutomatedCheck]:
    reliability = DimensionName.RELIABILITY
    completeness = DimensionName.COMPLETENESS
    usability = DimensionName.USABILITY
    checks = [
        _check(
            "artifact.checksum",
            reliability,
            actual_sha256 == case.artifact_sha256,
            "Artifact bytes match the frozen dataset checksum."
            if actual_sha256 == case.artifact_sha256
            else "Artifact checksum differs from the frozen dataset.",
            critical=True,
            refs=(case.artifact_path,),
        ),
        _check(
            "identity.event",
            reliability,
            raw.get("ticker") == case.ticker and raw.get("event_id") == case.event_id,
            "Ticker and event identity match the dataset."
            if raw.get("ticker") == case.ticker and raw.get("event_id") == case.event_id
            else "Ticker or event identity differs from the dataset.",
            critical=True,
        ),
    ]
    try:
        knowledge = datetime.fromisoformat(str(raw["knowledge_cutoff_at"]))
        available = datetime.fromisoformat(str(raw["available_at"]))
        target = datetime.fromisoformat(str(raw["target_cutoff_at"]))
        cutoff_safe = knowledge <= available < target
    except (KeyError, TypeError, ValueError):
        cutoff_safe = False
    checks.append(
        _check(
            "temporal.cutoff",
            reliability,
            cutoff_safe,
            "Knowledge and availability precede the target cutoff."
            if cutoff_safe
            else "Knowledge/availability timestamps are missing, malformed, or cross the target cutoff.",
            critical=True,
        )
    )
    canonical = (
        raw.get("canonical_card") if isinstance(raw.get("canonical_card"), dict) else {}
    )
    quarters = (
        canonical.get("historical_quarters") if isinstance(canonical, dict) else None
    )
    quarter = (
        quarters[0]
        if isinstance(quarters, list)
        and len(quarters) == 1
        and isinstance(quarters[0], dict)
        else {}
    )
    actual_results = quarter.get("actual_results") if isinstance(quarter, dict) else {}
    metrics = actual_results.get("metrics") if isinstance(actual_results, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    metric_failures: list[str] = []
    for expected in case.expected_metrics:
        observed = metrics.get(expected.metric)
        if not isinstance(observed, dict):
            metric_failures.append(f"{expected.metric}:missing")
            continue
        observed_value = observed.get("value")
        try:
            value_matches = Decimal(str(observed_value)) == expected.value
        except (InvalidOperation, TypeError, ValueError):
            value_matches = False
        if not value_matches:
            metric_failures.append(f"{expected.metric}:value")
        if observed.get("unit") != expected.unit:
            metric_failures.append(f"{expected.metric}:unit")
        if observed.get("accounting_basis") != expected.accounting_basis:
            metric_failures.append(f"{expected.metric}:accounting_basis")
        fiscal = observed.get("fiscal_period")
        fiscal_label = None
        if isinstance(fiscal, dict):
            fiscal_label = (
                f"FY{fiscal.get('fiscal_year')}_Q{fiscal.get('fiscal_quarter')}"
            )
        if fiscal_label != case.fiscal_period:
            metric_failures.append(f"{expected.metric}:period")
    checks.append(
        _check(
            "financial.metrics",
            reliability,
            not metric_failures,
            "Expected values, units, periods, and accounting bases match authoritative inputs."
            if not metric_failures
            else "Metric mismatches: " + ", ".join(metric_failures),
            critical=True,
            refs=("authoritative-source-input",),
        )
    )
    consensus = canonical.get("consensus_state") if isinstance(canonical, dict) else {}
    consensus_explicit = isinstance(consensus, dict) and (
        consensus.get("status") != "missing" or bool(consensus.get("blockers"))
    )
    checks.append(
        _check(
            "missingness.consensus",
            completeness,
            consensus_explicit,
            "Consensus is populated or its absence is explicit."
            if consensus_explicit
            else "Consensus is missing without an explicit reason.",
            critical=True,
        )
    )
    guidance = (
        canonical.get("management_guidance_ledger")
        if isinstance(canonical, dict)
        else None
    )
    guidance_state = raw.get("management_guidance_availability")
    guidance_status = (
        guidance_state.get("status") if isinstance(guidance_state, dict) else None
    )
    guidance_reason = (
        guidance_state.get("reason") if isinstance(guidance_state, dict) else None
    )
    guidance_explicit = (guidance_status == "available" and bool(guidance)) or (
        guidance_status in {"unavailable", "retrieval_failed"}
        and not guidance
        and isinstance(guidance_reason, str)
        and bool(guidance_reason.strip())
    )
    checks.append(
        _check(
            "missingness.management_guidance",
            completeness,
            guidance_explicit,
            "Management guidance is present or its absence is explicit."
            if guidance_explicit
            else "Management guidance is empty without an explicit unavailable/retrieval-failure reason.",
            critical=True,
        )
    )
    binding = raw.get("news_evidence_binding")
    selected_news = 0
    if isinstance(binding, dict):
        selected_news = len(binding.get("selected_provider_summaries") or ()) + len(
            binding.get("selected_passages") or ()
        )
    checks.append(
        _check(
            "developments.pre_event",
            completeness,
            selected_news > 0,
            "At least one checksum-bound pre-event development is selected."
            if selected_news > 0
            else "No selected pre-event development is available.",
        )
    )
    provenance = raw.get("provenance_links")
    checks.append(
        _check(
            "provenance.resolution",
            reliability,
            isinstance(provenance, list) and bool(provenance),
            "Readable provenance links are present."
            if isinstance(provenance, list) and provenance
            else "No readable provenance link is present.",
            critical=True,
        )
    )
    sections = (
        raw.get("executive_summary"),
        raw.get("key_messages"),
        raw.get("metric_interpretation"),
        raw.get("reaction_interpretation"),
    )
    checks.append(
        _check(
            "structure.required_sections",
            usability,
            all(bool(item) for item in sections),
            "Required readable sections are populated."
            if all(bool(item) for item in sections)
            else "One or more required readable sections are empty.",
        )
    )
    decision = canonical.get("decision") if isinstance(canonical, dict) else {}
    no_recommendation = isinstance(decision, dict) and decision.get("action") is None
    checks.append(
        _check(
            "scope.no_thesis",
            usability,
            no_recommendation,
            "The context card does not emit an investment action."
            if no_recommendation
            else "The context card improperly emits an investment action.",
            critical=True,
        )
    )
    return checks


def evaluate_artifact(
    case: DatasetCase,
    artifact_root: str | Path,
    rubric: ContextCardRubric,
    rubric_sha256: str,
    review: HumanReview | None = None,
) -> ContextCardQualityResult:
    # Imported lazily because the historical builder reaches product modules
    # that import the learning package during normal application startup.
    from eventlens.context_cards.builders.historical import (
        HistoricalContextCardArtifact,
    )

    path = Path(artifact_root) / case.artifact_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    actual_sha256 = _sha256(path)
    checks = _raw_checks(raw, case, actual_sha256)
    try:
        HistoricalContextCardArtifact.model_validate(raw)
    except ValidationError as error:
        checks.append(
            _check(
                "artifact.schema",
                DimensionName.RELIABILITY,
                False,
                f"Historical artifact validation failed with {error.error_count()} error(s).",
                critical=True,
            )
        )
    else:
        checks.append(
            _check(
                "artifact.schema",
                DimensionName.RELIABILITY,
                True,
                "Historical artifact and canonical card schemas validate.",
                critical=True,
            )
        )
    checks.extend(
        (
            _check(
                "claims.support",
                DimensionName.RELIABILITY,
                None,
                "Review whether every material narrative claim is supported by the cited evidence.",
            ),
            _check(
                "history.comparison",
                DimensionName.COMPLETENESS,
                None,
                "Review whether financial actuals include decision-relevant historical comparisons.",
            ),
            _check(
                "risks.cash_flow",
                DimensionName.COMPLETENESS,
                None,
                "Review cash-flow, financing, capital-intensity, and financial-risk coverage.",
            ),
            _check(
                "drivers.operations",
                DimensionName.COMPLETENESS,
                None,
                "Review material company-specific operational drivers.",
            ),
            _check(
                "priority.materiality",
                DimensionName.RELEVANCE,
                None,
                "Review prioritization for future earnings, cash flows, valuation, and investment risk.",
            ),
            _check(
                "noise.generic",
                DimensionName.RELEVANCE,
                None,
                "Review generic, superficial, or redundant commentary.",
            ),
            _check(
                "consumer.efficiency",
                DimensionName.USABILITY,
                None,
                "Review concision, uncertainty markers, and efficient deeper-source access.",
            ),
        )
    )
    if review is not None and (
        review.case_id != case.case_id or review.artifact_sha256 != actual_sha256
    ):
        raise ValueError(
            "human review identity/checksum does not match evaluated artifact"
        )
    dimensions: dict[DimensionName, DimensionResult] = {}
    missing_review: list[DimensionName] = []
    total = Decimal(0)
    for name in DimensionName:
        dimension_checks = tuple(item for item in checks if item.dimension is name)
        reviewed = review.dimensions.get(name) if review else None
        if reviewed is None:
            missing_review.append(name)
            points = None
        else:
            points = (
                reviewed.score
                / Decimal(5)
                * rubric.dimensions[name].weight
                * Decimal(100)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total += points
        dimensions[name] = DimensionResult(
            weight=rubric.dimensions[name].weight,
            automated_checks=dimension_checks,
            review_score=reviewed.score if reviewed else None,
            weighted_points=points,
            review_rationale=reviewed.rationale if reviewed else None,
            review_evidence_refs=reviewed.evidence_refs if reviewed else (),
        )
    critical_failures = tuple(
        item.check_id
        for item in checks
        if item.critical and item.status is CheckStatus.FAIL
    )
    score = None if missing_review else total
    if critical_failures:
        status = EvaluationStatus.FAIL
    elif score is None:
        status = EvaluationStatus.NOT_SCORED
    elif score >= rubric.target_score:
        status = EvaluationStatus.PASS
    else:
        status = EvaluationStatus.BELOW_TARGET
    return ContextCardQualityResult(
        case_id=case.case_id,
        role=case.role,
        ticker=case.ticker,
        event_id=case.event_id,
        fiscal_period=case.fiscal_period,
        artifact_path=case.artifact_path,
        artifact_sha256=actual_sha256,
        source_input_sha256=case.source_input_sha256,
        rubric_sha256=rubric_sha256,
        status=status,
        score=score,
        target_score=rubric.target_score,
        critical_failures=critical_failures,
        dimensions=dimensions,
        missing_review_dimensions=tuple(missing_review),
    )


def build_report(
    dataset: ContextCardDataset,
    dataset_sha256: str,
    rubric: ContextCardRubric,
    rubric_sha256: str,
    artifact_root: str | Path,
    reviews: dict[str, HumanReview] | None = None,
) -> ContextCardQualityReport:
    reviews = reviews or {}
    results = tuple(
        evaluate_artifact(
            case, artifact_root, rubric, rubric_sha256, reviews.get(case.case_id)
        )
        for case in dataset.cases
    )
    hard_failures = tuple(
        f"{result.case_id}:{failure}"
        for result in results
        for failure in result.critical_failures
    )
    missing_inputs = tuple(
        f"{result.case_id}:human_review:{dimension.value}"
        for result in results
        for dimension in result.missing_review_dimensions
    )
    failed_checks = Counter(failure.split(":", 1)[-1] for failure in hard_failures)
    if failed_checks:
        largest = failed_checks.most_common(1)[0][0]
    else:
        review_counts = Counter(item.split(":")[-1] for item in missing_inputs)
        largest = (
            f"human_review:{review_counts.most_common(1)[0][0]}"
            if review_counts
            else "none"
        )
    improvements: list[str] = []
    if any("missingness.management_guidance" in item for item in hard_failures):
        improvements.append(
            "Record an explicit management-guidance value or unavailable/retrieval-failure reason in every historical card."
        )
    if missing_inputs:
        improvements.append(
            "Calibrate and complete human review against the frozen 0–5 anchors before claiming a weighted score."
        )
    if len({case.ticker for case in dataset.cases}) == 1:
        improvements.append(
            "Add a second issuer only when an authoritative cutoff-safe card fixture is available."
        )
    if not improvements:
        improvements.append(
            "Retain the current deterministic gate behavior and expand only from observed failures."
        )
    coverage_limit = (
        f"Dataset: {len({case.ticker for case in dataset.cases})} ticker(s), "
        f"{len(dataset.cases)} case(s); broader performance is not established."
    )
    return ContextCardQualityReport(
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset_sha256,
        rubric_sha256=rubric_sha256,
        results=results,
        status_counts=dict(Counter(item.status for item in results)),
        hard_failures=hard_failures,
        missing_inputs=missing_inputs,
        largest_bottleneck=largest,
        recommended_improvements=tuple(improvements[:3]),
        coverage_limitations=(
            coverage_limit,
            "Decision relevance and downstream usability remain unscored until an attributable human review is supplied.",
            "No accepted downstream recommendation artifact is available for paired recommendation-quality scoring.",
        ),
    )


def compare_reports(
    before: ContextCardQualityReport, after: ContextCardQualityReport
) -> ContextCardQualityComparison:
    if before.dataset_id != after.dataset_id:
        raise ValueError("comparison requires the same dataset ID")
    if before.rubric_sha256 != after.rubric_sha256:
        raise ValueError("comparison requires the same frozen rubric checksum")
    before_by_id = {item.case_id: item for item in before.results}
    after_by_id = {item.case_id: item for item in after.results}
    if set(before_by_id) != set(after_by_id):
        raise ValueError("comparison requires identical case IDs")
    comparisons: list[CaseComparison] = []
    for case_id in sorted(before_by_id):
        old, new = before_by_id[case_id], after_by_id[case_id]
        if old.source_input_sha256 != new.source_input_sha256:
            raise ValueError(f"comparison source inputs differ for {case_id}")
        old_failures, new_failures = (
            set(old.critical_failures),
            set(new.critical_failures),
        )
        delta = (
            None if old.score is None or new.score is None else new.score - old.score
        )
        comparisons.append(
            CaseComparison(
                case_id=case_id,
                before_artifact_sha256=old.artifact_sha256,
                after_artifact_sha256=new.artifact_sha256,
                score_delta=delta,
                resolved_failures=tuple(sorted(old_failures - new_failures)),
                new_failures=tuple(sorted(new_failures - old_failures)),
                status_changed=old.status != new.status,
            )
        )
    return ContextCardQualityComparison(
        dataset_id=before.dataset_id,
        rubric_sha256=before.rubric_sha256,
        cases=tuple(comparisons),
    )


def ticket_evaluation_result(
    report: ContextCardQualityReport, ticket_id: str
) -> dict[str, Any]:
    """Project a quality report into the stable ticket-evaluation interface."""

    benchmark = next(
        (result for result in report.results if result.role == "benchmark"), None
    )
    if benchmark is None:
        raise ValueError("context-card evaluation requires one benchmark result")
    if report.hard_failures:
        status = "FAIL"
    elif any(result.status is EvaluationStatus.NOT_SCORED for result in report.results):
        status = "BLOCKED"
    elif all(result.status is EvaluationStatus.PASS for result in report.results):
        status = "PASS"
    else:
        status = "FAIL"
    return {
        "ticket_id": ticket_id,
        "evaluation_contract_version": RUBRIC_VERSION,
        "status": status,
        "critical_failures": list(report.hard_failures),
        "score": float(benchmark.score) if benchmark.score is not None else None,
        "artifact_refs": ["context-card-eval.json", "context-card-eval.md"],
    }


def human_review_template(case: DatasetCase) -> dict[str, Any]:
    return {
        "schema_version": RUBRIC_VERSION,
        "case_id": case.case_id,
        "artifact_sha256": case.artifact_sha256,
        "reviewer": "REQUIRED",
        "reviewed_at": "REQUIRED_TIMEZONE_AWARE_TIMESTAMP",
        "dimensions": {
            dimension.value: {
                "score": None,
                "rationale": "REQUIRED",
                "evidence_refs": [],
            }
            for dimension in DimensionName
        },
    }


def render_report_markdown(report: ContextCardQualityReport) -> str:
    lines = [
        "# Context Card Eval v0.1 baseline",
        "",
        f"- Dataset: `{report.dataset_id}` (`{report.dataset_sha256}`)",
        f"- Rubric: `{report.rubric_version}` (`{report.rubric_sha256}`)",
        f"- Status counts: {', '.join(f'{key.value}={value}' for key, value in sorted(report.status_counts.items(), key=lambda item: item[0].value))}",
        f"- Largest bottleneck: `{report.largest_bottleneck}`",
        "",
        "## Cases",
        "",
    ]
    for result in report.results:
        score = "NOT SCORED" if result.score is None else f"{result.score}/100"
        lines.extend(
            [
                f"### {result.case_id}",
                "",
                f"- Status: **{result.status.value}**",
                f"- Score: {score}",
                f"- Artifact checksum: `{result.artifact_sha256}`",
                f"- Critical failures: {', '.join(result.critical_failures) if result.critical_failures else 'none'}",
                f"- Missing review: {', '.join(item.value for item in result.missing_review_dimensions) if result.missing_review_dimensions else 'none'}",
                "",
            ]
        )
        for dimension, dimension_result in result.dimensions.items():
            lines.append(f"#### {dimension.value}")
            lines.append("")
            lines.append(
                f"Review score: {dimension_result.review_score if dimension_result.review_score is not None else 'NOT SCORED'}"
            )
            for check in dimension_result.automated_checks:
                critical = " [CRITICAL]" if check.critical else ""
                lines.append(
                    f"- {check.status.value}{critical} `{check.check_id}` — {check.detail}"
                )
            lines.append("")
    lines.extend(["## Missing inputs", ""])
    lines.extend(f"- {item}" for item in report.missing_inputs)
    lines.extend(["", "## Top recommended improvements", ""])
    lines.extend(
        f"{index}. {item}"
        for index, item in enumerate(report.recommended_improvements, 1)
    )
    lines.extend(["", "## Coverage limitations", ""])
    lines.extend(f"- {item}" for item in report.coverage_limitations)
    return "\n".join(lines).rstrip() + "\n"


def render_comparison_markdown(comparison: ContextCardQualityComparison) -> str:
    lines = [
        "# Context Card Eval v0.1 comparison",
        "",
        f"- Dataset: `{comparison.dataset_id}`",
        f"- Rubric checksum: `{comparison.rubric_sha256}`",
        "",
    ]
    for case in comparison.cases:
        lines.extend(
            [
                f"## {case.case_id}",
                "",
                f"- Score delta: {case.score_delta if case.score_delta is not None else 'NOT SCORED'}",
                f"- Resolved failures: {', '.join(case.resolved_failures) if case.resolved_failures else 'none'}",
                f"- New failures: {', '.join(case.new_failures) if case.new_failures else 'none'}",
                f"- Status changed: {str(case.status_changed).lower()}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "COMPARISON_VERSION",
    "DATASET_VERSION",
    "REPORT_VERSION",
    "RUBRIC_VERSION",
    "ContextCardDataset",
    "ContextCardQualityComparison",
    "ContextCardQualityReport",
    "ContextCardQualityResult",
    "ContextCardRubric",
    "HumanReview",
    "build_report",
    "compare_reports",
    "human_review_template",
    "load_dataset",
    "load_review",
    "load_rubric",
    "render_comparison_markdown",
    "render_report_markdown",
    "ticket_evaluation_result",
]
