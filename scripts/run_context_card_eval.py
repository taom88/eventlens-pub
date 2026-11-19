"""Run local Context Card Eval v0.1 baselines and frozen-rubric comparisons."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eventlens.learning.context_card_quality import (
    ContextCardQualityReport,
    build_report,
    compare_reports,
    human_review_template,
    load_dataset,
    load_review,
    load_rubric,
    render_comparison_markdown,
    render_report_markdown,
    ticket_evaluation_result,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUBRIC = ROOT / "docs" / "evaluations" / "context-card-rubric-v0.1.json"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _reviews(root: Path | None) -> dict[str, Any]:
    if root is None or not root.exists():
        return {}
    return {path.stem: load_review(path) for path in sorted(root.glob("*.json"))}


def _baseline(args: argparse.Namespace) -> int:
    rubric, rubric_sha256 = load_rubric(args.rubric)
    dataset, dataset_sha256 = load_dataset(args.dataset)
    reviews = _reviews(args.review_dir)
    report = build_report(
        dataset,
        dataset_sha256,
        rubric,
        rubric_sha256,
        args.artifact_root,
        reviews,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "context-card-eval.json", report)
    (args.output_dir / "context-card-eval.md").write_text(
        render_report_markdown(report), encoding="utf-8"
    )
    if args.ticket_id:
        _write_json(
            args.output_dir / "ticket-evaluation-result.json",
            ticket_evaluation_result(report, args.ticket_id),
        )
    template_root = args.output_dir / "review-templates"
    for case in dataset.cases:
        if case.case_id not in reviews:
            _write_json(
                template_root / f"{case.case_id}.json", human_review_template(case)
            )
    print(
        f"Context Card Eval v0.1: {len(report.results)} cases; "
        f"{len(report.hard_failures)} hard failures; "
        f"{len(report.missing_inputs)} missing review inputs."
    )
    print(args.output_dir / "context-card-eval.json")
    print(args.output_dir / "context-card-eval.md")
    return 1 if report.hard_failures and args.fail_on_critical else 0


def _compare(args: argparse.Namespace) -> int:
    before = ContextCardQualityReport.model_validate_json(
        args.before.read_text(encoding="utf-8")
    )
    after = ContextCardQualityReport.model_validate_json(
        args.after.read_text(encoding="utf-8")
    )
    comparison = compare_reports(before, after)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "context-card-comparison.json", comparison)
    (args.output_dir / "context-card-comparison.md").write_text(
        render_comparison_markdown(comparison), encoding="utf-8"
    )
    print(args.output_dir / "context-card-comparison.json")
    print(args.output_dir / "context-card-comparison.md")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    baseline = subparsers.add_parser("baseline", help="evaluate one dataset")
    baseline.add_argument("--dataset", type=Path, required=True)
    baseline.add_argument("--artifact-root", type=Path, required=True)
    baseline.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    baseline.add_argument("--review-dir", type=Path)
    baseline.add_argument("--output-dir", type=Path, required=True)
    baseline.add_argument("--fail-on-critical", action="store_true")
    baseline.add_argument(
        "--ticket-id",
        help="also emit the stable ticket-evaluation result for EL-NNN",
    )
    compare = subparsers.add_parser("compare", help="compare compatible reports")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return _baseline(args) if args.command == "baseline" else _compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
