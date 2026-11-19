# Context Card Eval v0.1 improvement loop

Context Card Eval v0.1 is a local, key-free quality contract for tickets that
change historical context-card production or consumption. The checked-in public
fixture is synthetic and only supports an evaluator smoke run; it is not a
human-reviewed quality benchmark. An applicable ticket
declares:

```text
**Evaluation contract:** eventlens.context-card-eval.v0.1
```

The ticket must name the frozen dataset and rubric. The evaluator reads private
artifacts but writes only checksums, structured findings, and public-safe
descriptions. It never changes card approval or creates an investment thesis.

## Establish or reproduce a baseline

```bash
.venv/bin/python scripts/run_context_card_eval.py baseline \
  --dataset fixtures/evals/context_card_quality_v0_1/demo-v1.json \
  --artifact-root fixtures/evals/context_card_quality_v0_1 \
  --output-dir /tmp/context-card-eval
```

The synthetic smoke run should report one case, zero critical failures, and four
missing review inputs. Do not treat its result as evidence of card quality.
The command emits one JSON report, one Markdown report, and incomplete
human-review templates. When a caller supplies `--ticket-id`, it also emits a
stable ticket-result JSON adapter. A critical failure makes that adapter `FAIL`.
Missing qualitative review without a critical failure is `BLOCKED`, not a
fabricated score.

## Review and compare a candidate

An attributable reviewer may complete every dimension in a generated template
using the frozen 0–5 anchors and evidence references. Re-run the baseline with
`--review-dir`, then compare compatible reports:

```bash
.venv/bin/python scripts/run_context_card_eval.py compare \
  --before /tmp/context-card-before/context-card-eval.json \
  --after /tmp/context-card-after/context-card-eval.json \
  --output-dir /tmp/context-card-comparison
```

Comparison requires the same dataset ID, rubric checksum, case IDs, and source
input checksums. Artifact checksums may change. The report identifies resolved
and introduced critical failures and any reviewed score delta.

Use the largest observed bottleneck to scope one successor ticket. Do not tune
the rubric to make a candidate pass, infer subjective scores, or expand the
dataset without authoritative cutoff-safe evidence. Re-run tests and this eval
after the change, then attach the adapter through the canonical completion
command. Hermes or another reviewer may propose findings, but has no write or
approval authority.

Recommendation-quality evaluation remains deferred until EventLens has an
accepted, checksum-bound downstream recommendation artifact.
