import json
from datetime import date
from pathlib import Path

from eventlens.providers.fixture import FixtureProvider
from eventlens.quant.backtest import (
    canonical_event_study_bytes,
    event_study_report_sha256,
    run_event_study,
)


def test_demo_event_study_golden_report_is_byte_stable() -> None:
    root = Path(__file__).parents[2]
    golden = json.loads((root / "fixtures/evals/demo-event-study-golden.json").read_text())
    provider = FixtureProvider(root / "fixtures")
    events = provider.get_earnings_events("DEMO", date(2024, 1, 1), date(2026, 12, 31)).data.events
    history = provider.get_daily_history("DEMO", date(2024, 1, 1), date(2026, 12, 31)).data

    first = run_event_study("DEMO", events, history)
    second = run_event_study("DEMO", events, history)
    first_bytes = canonical_event_study_bytes(first)
    second_bytes = canonical_event_study_bytes(second)

    assert first_bytes == second_bytes
    assert first.ticker == golden["ticker"]
    assert first.event_count == golden["event_count"]
    assert first.calculation_version == golden["calculation_version"]
    assert first.input_sha256 == golden["input_sha256"]
    assert event_study_report_sha256(first) == golden["report_json_sha256"]
