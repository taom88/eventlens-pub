from datetime import date
from decimal import Decimal

import pytest

from eventlens.learning.dataset import temporal_ticker_disjoint_split
from eventlens.learning.models import (
    EvaluationExample,
    EvaluationResult,
    EvaluationStatus,
)
from eventlens.learning.scorecards import build_scorecard


def _example(ticker: str, day: int) -> EvaluationExample:
    return EvaluationExample.model_construct(
        schema_version="1.0",
        example_id=f"{ticker}-{day}",
        ticker=ticker,
        event_date=date(2026, 1, day),
    )


def test_temporal_split_is_ticker_disjoint_and_reproducible() -> None:
    examples = tuple(_example(ticker, index + 1) for index, ticker in enumerate(("A", "B", "C", "D", "E")))
    first = temporal_ticker_disjoint_split(examples)
    second = temporal_ticker_disjoint_split(reversed(examples))
    assert first == second
    ticker_sets = [
        {item.ticker for item in first[name]}
        for name in ("train", "validation", "test")
    ]
    assert not (ticker_sets[0] & ticker_sets[1])
    assert not (ticker_sets[0] & ticker_sets[2])
    assert not (ticker_sets[1] & ticker_sets[2])


def test_split_requires_enough_independent_tickers() -> None:
    with pytest.raises(ValueError, match="three ticker groups"):
        temporal_ticker_disjoint_split((_example("A", 1), _example("A", 2)))


def test_scorecard_reports_uncertainty_and_tiny_sample_warning() -> None:
    results = (
        EvaluationResult(
            evaluation_name="citation_correctness",
            grader_version="v1",
            example_id="a",
            status=EvaluationStatus.PASS,
            score=Decimal("1"),
            detail="ok",
        ),
        EvaluationResult(
            evaluation_name="citation_correctness",
            grader_version="v1",
            example_id="b",
            status=EvaluationStatus.FAIL,
            score=Decimal("0"),
            detail="bad",
        ),
    )
    row = build_scorecard(
        results,
        dimension="provider",
        keys_by_example={"a": "provider-a", "b": "provider-a"},
    )[0]
    assert row.sample_size == 2
    assert row.mean_score == Decimal("0.5")
    assert row.tiny_sample_warning is True
    assert Decimal("0") <= row.lower_bound <= row.upper_bound <= Decimal("1")
