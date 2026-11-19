"""Hostile-fixture tests: fixture content must never be silently relabeled."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pytest

from eventlens.providers.errors import ProviderPayloadError
from eventlens.providers.fixture import FixtureProvider
from eventlens.providers.models import DataQualityStatus


def _document() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "ticker": "DEMO",
        "retrieved_at": "2026-07-18T12:00:00+00:00",
        "market_history": {
            "ticker": "DEMO",
            "bars": [
                {
                    "trading_date": "2026-07-02",
                    "open": "100",
                    "high": "102",
                    "low": "99",
                    "close": "101",
                    "adjusted_close": "101",
                    "volume": 50,
                }
            ],
        },
        "earnings_events": {
            "ticker": "DEMO",
            "events": [
                {
                    "ticker": "DEMO",
                    "event_date": "2026-07-02",
                    "fiscal_quarter": "2026Q4",
                    "source_event_id": "demo-2026q4",
                }
            ],
        },
        "transcripts": {
            "2026Q4": {
                "ticker": "DEMO",
                "fiscal_quarter": "2026Q4",
                "event_date": "2026-07-02",
                "passages": [{"passage_id": "p1", "text": "Fixture evidence."}],
            }
        },
        "option_snapshots": {
            "2026-07-02": {
                "ticker": "DEMO",
                "snapshot_date": "2026-07-02",
                "as_of": "2026-07-02T19:59:00+00:00",
                "snapshot_kind": "end_of_day",
                "pre_event_alignment": "verified_pre_event",
                "spot": "100",
                "source": "fixture_options",
                "quotes": [
                    {
                        "option_type": "call",
                        "strike": "100",
                        "expiration": "2026-07-10",
                        "bid": "2",
                        "ask": "2.2",
                    },
                    {
                        "option_type": "put",
                        "strike": "100",
                        "expiration": "2026-07-10",
                        "bid": "2",
                        "ask": "2.2",
                    },
                ],
            }
        },
    }


def _provider(tmp_path: Path, document: dict[str, Any]) -> FixtureProvider:
    (tmp_path / "DEMO.json").write_text(json.dumps(document), encoding="utf-8")
    return FixtureProvider(tmp_path)


def test_fixture_rejects_bundle_ticker_mismatch(tmp_path: Path) -> None:
    document = _document()
    document["ticker"] = "AAPL"

    with pytest.raises(ProviderPayloadError, match="fixture.ticker ticker AAPL"):
        _provider(tmp_path, document).get_daily_history(
            "DEMO", date(2026, 7, 2), date(2026, 7, 2)
        )


@pytest.mark.parametrize(
    ("mutate", "load", "expected"),
    [
        (
            lambda item: item["market_history"].__setitem__("ticker", "AAPL"),
            lambda provider: provider.get_daily_history(
                "DEMO", date(2026, 7, 2), date(2026, 7, 2)
            ),
            "market_history.ticker ticker AAPL",
        ),
        (
            lambda item: item["earnings_events"]["events"][0].__setitem__("ticker", "AAPL"),
            lambda provider: provider.get_earnings_events(
                "DEMO", date(2026, 7, 2), date(2026, 7, 2)
            ),
            "earnings_events.events[demo-2026q4].ticker ticker AAPL",
        ),
        (
            lambda item: item["transcripts"]["2026Q4"].__setitem__("ticker", "AAPL"),
            lambda provider: provider.get_transcript("DEMO", "2026Q4"),
            "transcripts[2026Q4].ticker ticker AAPL",
        ),
        (
            lambda item: item["option_snapshots"]["2026-07-02"].__setitem__("ticker", "AAPL"),
            lambda provider: provider.get_historical_options("DEMO", date(2026, 7, 2)),
            "option_snapshots[2026-07-02].ticker ticker AAPL",
        ),
    ],
)
def test_fixture_rejects_every_nested_ticker_mismatch(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    load: Callable[[FixtureProvider], object],
    expected: str,
) -> None:
    document = _document()
    mutate(document)

    with pytest.raises(ProviderPayloadError, match=re.escape(expected)):
        load(_provider(tmp_path, document))


def test_fixture_rejects_transcript_key_and_quarter_mismatch(tmp_path: Path) -> None:
    document = _document()
    document["transcripts"]["2026Q4"]["fiscal_quarter"] = "2026Q3"

    with pytest.raises(ProviderPayloadError, match="fiscal_quarter must equal its fixture key"):
        _provider(tmp_path, document).get_transcript("DEMO", "2026Q4")


def test_fixture_rejects_option_snapshot_date_mismatch(tmp_path: Path) -> None:
    document = _document()
    document["option_snapshots"]["2026-07-02"]["snapshot_date"] = "2026-07-01"

    with pytest.raises(ProviderPayloadError, match="snapshot_date must equal"):
        _provider(tmp_path, document).get_historical_options("DEMO", date(2026, 7, 2))


@pytest.mark.parametrize(
    "load",
    [
        lambda provider: provider.get_daily_history(
            "DEMO", date(2026, 7, 3), date(2026, 7, 2)
        ),
        lambda provider: provider.get_earnings_events(
            "DEMO", date(2026, 7, 3), date(2026, 7, 2)
        ),
    ],
)
def test_fixture_rejects_inverted_requested_date_ranges(
    tmp_path: Path, load: Callable[[FixtureProvider], object]
) -> None:
    with pytest.raises(ValueError, match="start must be on or before end"):
        load(_provider(tmp_path, _document()))


def test_fixture_reports_actual_partial_coverage_instead_of_requested_window(
    tmp_path: Path,
) -> None:
    result = _provider(tmp_path, _document()).get_daily_history(
        "DEMO", date(2026, 7, 1), date(2026, 7, 3)
    )

    assert result.provenance.coverage_start == date(2026, 7, 2)
    assert result.provenance.coverage_end == date(2026, 7, 2)
    assert result.provenance.data_quality is DataQualityStatus.PARTIAL
    assert result.provenance.warnings[0].code == "requested_window_partial"


def test_fixture_marks_empty_requested_window_unavailable(tmp_path: Path) -> None:
    result = _provider(tmp_path, _document()).get_earnings_events(
        "DEMO", date(2026, 7, 3), date(2026, 7, 4)
    )

    assert result.provenance.coverage_start is None
    assert result.provenance.coverage_end is None
    assert result.provenance.data_quality is DataQualityStatus.UNAVAILABLE
    assert result.provenance.warnings[0].code == "requested_window_unavailable"
