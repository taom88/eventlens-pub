from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from eventlens.providers.fixture import FixtureProvider
from eventlens.quant.inputs import (
    AnalyticsEligibility,
    assemble_event_analytics_inputs,
    run_event_study_from_bundle,
)


def _responses():
    provider = FixtureProvider(Path("fixtures"))
    events = provider.get_earnings_events("DEMO", date(2020, 1, 1), date(2030, 1, 1))
    history = provider.get_daily_history("DEMO", date(2020, 1, 1), date(2030, 1, 1))
    return events, history


def test_manifest_retains_provenance_and_requires_degraded_approval() -> None:
    events, history = _responses()
    cutoff = datetime(2026, 1, 6, 21, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="explicit approval"):
        assemble_event_analytics_inputs(
            events, history, information_cutoff_at=cutoff
        )
    bundle = assemble_event_analytics_inputs(
        events, history, information_cutoff_at=cutoff, allow_degraded=True
    )
    assert bundle.manifest.eligibility is AnalyticsEligibility.DEGRADED_APPROVED
    assert {item.role.value for item in bundle.manifest.receipts} == {"events", "ohlcv"}
    assert bundle.manifest.receipts[0].origin.value == "fixture"
    assert bundle.manifest.receipts[0].quota_consumed is False


def test_identical_manifest_runs_are_stable_and_report_is_unchanged() -> None:
    events, history = _responses()
    kwargs = {
        "information_cutoff_at": datetime(2026, 1, 6, 21, tzinfo=timezone.utc),
        "allow_degraded": True,
    }
    first = assemble_event_analytics_inputs(events, history, **kwargs)
    second = assemble_event_analytics_inputs(events, history, **kwargs)
    assert first.manifest == second.manifest
    first_report, first_manifest = run_event_study_from_bundle(first)
    second_report, second_manifest = run_event_study_from_bundle(second)
    assert first_manifest.normalized_input_sha256 == second_manifest.normalized_input_sha256
    assert first_report == second_report


def test_manifest_hash_changes_when_policy_changes() -> None:
    events, history = _responses()
    cutoff = datetime(2026, 1, 6, 21, tzinfo=timezone.utc)
    first = assemble_event_analytics_inputs(
        events, history, information_cutoff_at=cutoff, allow_degraded=True,
        policy_version="policy-a",
    )
    second = assemble_event_analytics_inputs(
        events, history, information_cutoff_at=cutoff, allow_degraded=True,
        policy_version="policy-b",
    )
    assert first.manifest.normalized_input_sha256 != second.manifest.normalized_input_sha256


def test_manifest_rejects_mismatched_tickers() -> None:
    events, history = _responses()
    with pytest.raises(ValueError, match="tickers must match"):
        assemble_event_analytics_inputs(
            events,
            history.model_copy(update={"data": history.data.model_copy(update={"ticker": "OTHER"})}),
            information_cutoff_at=datetime(2026, 1, 6, 21, tzinfo=timezone.utc),
            allow_degraded=True,
        )
