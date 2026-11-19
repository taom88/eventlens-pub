from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from eventlens.context_cards.builders import build_historical_context_card
from eventlens.learning.context_card_quality import (
    CheckStatus,
    ContextCardDataset,
    DatasetCase,
    DimensionName,
    EvaluationStatus,
    ExpectedMetric,
    HumanReview,
    ReviewScore,
    build_report,
    compare_reports,
    evaluate_artifact,
    load_dataset,
    load_rubric,
    render_report_markdown,
    ticket_evaluation_result,
)
from tests.context_cards.test_historical_builder import _input

ROOT = Path(__file__).parents[2]
RUBRIC_PATH = ROOT / "docs/evaluations/context-card-rubric-v0.1.json"
DATASET_PATH = ROOT / "fixtures/evals/context_card_quality_v0_1/demo-v1.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case(artifact_path: Path, artifact_sha256: str) -> DatasetCase:
    return DatasetCase(
        case_id="demo-fy2026-q2",
        role="benchmark",
        ticker="DEMO",
        event_id="DEMO:2026-08-10:FY2026Q2",
        fiscal_period="FY2026_Q2",
        artifact_path=artifact_path.name,
        artifact_sha256=artifact_sha256,
        source_input_sha256="f" * 64,
        expected_metrics=(
            ExpectedMetric(
                metric="revenue",
                value=Decimal("123.456"),
                unit="USD_million",
                accounting_basis="gaap",
            ),
        ),
    )


def _write_artifact(
    tmp_path: Path, *, explicit_guidance_state: bool = False
) -> tuple[Path, DatasetCase]:
    artifact = build_historical_context_card(_input())
    path = tmp_path / "historical-context-card.json"
    raw = artifact.model_dump(mode="json")
    if not explicit_guidance_state:
        raw.pop("management_guidance_availability")
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    return path, _case(path, _sha256(path))


def _check_ids(result, *, status: CheckStatus) -> set[str]:
    return {
        check.check_id
        for dimension in result.dimensions.values()
        for check in dimension.automated_checks
        if check.status is status
    }


def _review(case: DatasetCase) -> HumanReview:
    return HumanReview(
        case_id=case.case_id,
        artifact_sha256=case.artifact_sha256,
        reviewer="owner-analyst",
        reviewed_at=datetime(2026, 11, 15, 18, tzinfo=UTC),
        dimensions={
            dimension: ReviewScore(
                score=Decimal(5),
                rationale="Reviewed against the frozen anchor and cited artifact.",
                evidence_refs=(case.artifact_path,),
            )
            for dimension in DimensionName
        },
    )


def test_versioned_rubric_and_synthetic_dataset_are_bounded_and_parseable() -> None:
    rubric, rubric_sha256 = load_rubric(RUBRIC_PATH)
    dataset, dataset_sha256 = load_dataset(DATASET_PATH)

    assert rubric.target_score == Decimal(85)
    assert sum(item.weight for item in rubric.dimensions.values()) == 1
    assert len(rubric_sha256) == len(dataset_sha256) == 64
    assert [case.role for case in dataset.cases] == ["benchmark"]
    assert {case.ticker for case in dataset.cases} == {"DEMO"}
    case = dataset.cases[0]
    artifact = DATASET_PATH.parent / case.artifact_path
    assert artifact.is_file()
    assert _sha256(artifact) == case.artifact_sha256


def test_unreviewed_card_reports_hard_missingness_and_not_scored_dimensions(
    tmp_path: Path,
) -> None:
    _, case = _write_artifact(tmp_path)
    rubric, rubric_sha256 = load_rubric(RUBRIC_PATH)

    result = evaluate_artifact(case, tmp_path, rubric, rubric_sha256)

    assert result.status is EvaluationStatus.FAIL
    assert result.score is None
    assert result.missing_review_dimensions == tuple(DimensionName)
    assert "missingness.management_guidance" in result.critical_failures
    assert "financial.metrics" in _check_ids(result, status=CheckStatus.PASS)
    assert "claims.support" in _check_ids(result, status=CheckStatus.REVIEW)


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    (
        ("missing_consensus", "missingness.consensus"),
        ("gaap_confusion", "financial.metrics"),
        ("look_ahead", "temporal.cutoff"),
    ),
)
def test_critical_regressions_fail_closed(
    tmp_path: Path, mutation: str, expected_failure: str
) -> None:
    path, case = _write_artifact(tmp_path, explicit_guidance_state=True)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing_consensus":
        raw["canonical_card"]["consensus_state"]["blockers"] = []
    elif mutation == "gaap_confusion":
        raw["canonical_card"]["historical_quarters"][0]["actual_results"]["metrics"][
            "revenue"
        ]["accounting_basis"] = "non_gaap"
    else:
        raw["available_at"] = raw["target_cutoff_at"]
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    case = case.model_copy(update={"artifact_sha256": _sha256(path)})
    rubric, rubric_sha256 = load_rubric(RUBRIC_PATH)

    result = evaluate_artifact(case, tmp_path, rubric, rubric_sha256)

    assert result.status is EvaluationStatus.FAIL
    assert expected_failure in result.critical_failures


def test_reviewed_score_and_comparison_preserve_hard_gate_priority(
    tmp_path: Path,
) -> None:
    _, failing_case = _write_artifact(tmp_path)
    rubric, rubric_sha256 = load_rubric(RUBRIC_PATH)
    failing_dataset = ContextCardDataset(
        dataset_id="test-dataset",
        description="One deterministic card.",
        source_input_path="fixture",
        cases=(failing_case,),
    )
    before = build_report(
        failing_dataset,
        "a" * 64,
        rubric,
        rubric_sha256,
        tmp_path,
        {failing_case.case_id: _review(failing_case)},
    )
    assert "1 ticker(s), 1 case(s)" in before.coverage_limitations[0]
    assert before.results[0].score == Decimal(100)
    assert before.results[0].status is EvaluationStatus.FAIL

    _, passing_case = _write_artifact(tmp_path, explicit_guidance_state=True)
    passing_dataset = failing_dataset.model_copy(update={"cases": (passing_case,)})
    after = build_report(
        passing_dataset,
        "b" * 64,
        rubric,
        rubric_sha256,
        tmp_path,
        {passing_case.case_id: _review(passing_case)},
    )
    assert after.results[0].status is EvaluationStatus.PASS
    comparison = compare_reports(before, after)
    assert comparison.cases[0].resolved_failures == ("missingness.management_guidance",)
    assert comparison.cases[0].score_delta == 0

    mismatched = after.model_copy(
        update={
            "results": (
                after.results[0].model_copy(update={"source_input_sha256": "0" * 64}),
            )
        }
    )
    with pytest.raises(ValueError, match="source inputs differ"):
        compare_reports(before, mismatched)


def test_report_exposes_bottleneck_improvements_and_public_safe_markdown(
    tmp_path: Path,
) -> None:
    _, case = _write_artifact(tmp_path)
    rubric, rubric_sha256 = load_rubric(RUBRIC_PATH)
    dataset = ContextCardDataset(
        dataset_id="test-dataset",
        description="One deterministic card.",
        source_input_path="fixture",
        cases=(case,),
    )

    report = build_report(dataset, "a" * 64, rubric, rubric_sha256, tmp_path)
    markdown = render_report_markdown(report)

    assert report.largest_bottleneck == "missingness.management_guidance"
    assert len(report.recommended_improvements) == 3
    assert "NOT SCORED" in markdown
    assert "provider summary" not in markdown.lower()
    ticket_result = ticket_evaluation_result(report, "EL-023")
    assert ticket_result["status"] == "FAIL"
    assert ticket_result["score"] is None
    assert ticket_result["evaluation_contract_version"] == (
        "eventlens.context-card-eval.v0.1"
    )
