"""Conservative scorecard aggregation with visible sample uncertainty."""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal

from .models import EvaluationResult, ScorecardRow


def build_scorecard(
    results: tuple[EvaluationResult, ...],
    *,
    dimension: str,
    keys_by_example: dict[str, str],
    tiny_sample_threshold: int = 30,
) -> tuple[ScorecardRow, ...]:
    grouped: dict[str, list[Decimal]] = defaultdict(list)
    for result in results:
        if result.score is not None and result.example_id in keys_by_example:
            grouped[keys_by_example[result.example_id]].append(result.score)
    rows = []
    for key in sorted(grouped):
        scores = grouped[key]
        n = len(scores)
        mean = sum(scores, Decimal("0")) / n
        if n > 1:
            variance = sum(((item - mean) ** 2 for item in scores), Decimal("0")) / (n - 1)
            margin = Decimal(str(1.96 * math.sqrt(float(variance) / n)))
        else:
            margin = Decimal("1")
        rows.append(
            ScorecardRow(
                dimension=dimension,
                key=key,
                sample_size=n,
                mean_score=mean,
                lower_bound=max(Decimal("0"), mean - margin),
                upper_bound=min(Decimal("1"), mean + margin),
                tiny_sample_warning=n < tiny_sample_threshold,
            )
        )
    return tuple(rows)
