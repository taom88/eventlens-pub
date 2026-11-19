from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from eventlens.context_cards import (
    AvailabilityValue,
    FactTruthStatus,
    TemporalClassification,
    TemporalConfidence,
    TemporalContext,
    assert_cutoff_safe,
    classify_temporal,
)


UTC = timezone.utc


def _native_source(
    *,
    source_published_at: datetime | str | None,
    provider_observed_at: datetime | str,
    first_known_available_at: datetime | str | None,
    truth_status: FactTruthStatus = FactTruthStatus.VERIFIED,
) -> dict[str, object]:
    return {
        "source_published_at": source_published_at,
        "provider_observed_at": provider_observed_at,
        "first_known_available_at": first_known_available_at,
        "is_reconstructed": False,
        "reconstructed_at": None,
        "availability_evidence": ("issuer_or_regulator_timestamp",),
        "fact_truth_status": truth_status,
    }


def test_pre_event_sec_filing_is_native_available_and_high_confidence() -> None:
    filed_at = datetime(2025, 1, 29, 21, 6, tzinfo=UTC)
    cutoff = datetime(2025, 4, 30, 20, 0, tzinfo=UTC)

    context = classify_temporal(
        _native_source(
            source_published_at=filed_at,
            provider_observed_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
            first_known_available_at=filed_at,
        ),
        cutoff,
    )

    assert context.available_before_event.value is AvailabilityValue.AVAILABLE
    assert context.classification is TemporalClassification.NATIVE
    assert context.temporal_confidence is TemporalConfidence.HIGH
    assert context.fact_truth_status is FactTruthStatus.VERIFIED
    assert context.historical_event_cutoff == cutoff
    assert context.reconstruction.is_reconstructed is False
    assert context.reconstruction.reconstructed_at is None
    assert assert_cutoff_safe(context) is context


def test_present_day_historical_consensus_is_reconstructed_unknown_and_low() -> None:
    observed_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    context = classify_temporal(
        {
            "source_published_at": None,
            "provider_observed_at": observed_at,
            "first_known_available_at": None,
            "reconstruction": {
                "is_reconstructed": True,
                "reconstructed_at": observed_at,
            },
            "availability_evidence": ("provider_query_performed_today",),
            "fact_truth_status": FactTruthStatus.REPORTED,
        },
        datetime(2025, 1, 29, 20, 0, tzinfo=UTC),
    )

    assert context.available_before_event.value is AvailabilityValue.UNKNOWN
    assert context.classification is TemporalClassification.RECONSTRUCTED
    assert context.temporal_confidence is TemporalConfidence.LOW
    assert context.fact_truth_status is FactTruthStatus.REPORTED
    with pytest.raises(ValueError, match="not cutoff-safe"):
        assert_cutoff_safe(context)


def test_earnings_release_availability_changes_with_declared_cutoff() -> None:
    released_at = datetime(2025, 1, 29, 21, 5, tzinfo=UTC)
    source = _native_source(
        source_published_at=released_at,
        provider_observed_at=released_at,
        first_known_available_at=released_at,
    )

    own_event = classify_temporal(
        source,
        datetime(2025, 1, 29, 21, 0, tzinfo=UTC),
    )
    later_event = classify_temporal(
        source,
        datetime(2025, 4, 30, 20, 0, tzinfo=UTC),
    )

    assert own_event.available_before_event.value is AvailabilityValue.UNAVAILABLE
    assert later_event.available_before_event.value is AvailabilityValue.AVAILABLE
    with pytest.raises(ValueError, match="availability=unavailable"):
        assert_cutoff_safe(own_event)
    assert_cutoff_safe(later_event)


def test_truth_status_does_not_change_temporal_availability() -> None:
    timestamp = datetime(2025, 1, 20, 15, 0, tzinfo=UTC)
    source = _native_source(
        source_published_at=timestamp,
        provider_observed_at=timestamp,
        first_known_available_at=timestamp,
        truth_status=FactTruthStatus.UNKNOWN,
    )

    context = classify_temporal(
        source,
        datetime(2025, 1, 29, 21, 0, tzinfo=UTC),
    )

    assert context.fact_truth_status is FactTruthStatus.UNKNOWN
    assert context.available_before_event.value is AvailabilityValue.AVAILABLE


@pytest.mark.parametrize(
    "field_name",
    ["source_published_at", "provider_observed_at", "first_known_available_at"],
)
def test_date_only_same_day_timestamp_is_rejected(field_name: str) -> None:
    exact_time = datetime(2025, 1, 29, 19, 0, tzinfo=UTC)
    source = _native_source(
        source_published_at=exact_time,
        provider_observed_at=exact_time,
        first_known_available_at=exact_time,
    )
    source[field_name] = "2025-01-29"

    with pytest.raises(ValueError, match="exact timezone-aware time"):
        classify_temporal(source, datetime(2025, 1, 29, 21, 0, tzinfo=UTC))


def test_timestamp_at_cutoff_is_not_assumed_pre_cutoff() -> None:
    cutoff = datetime(2025, 1, 29, 21, 0, tzinfo=UTC)
    context = classify_temporal(
        _native_source(
            source_published_at=cutoff,
            provider_observed_at=cutoff,
            first_known_available_at=cutoff,
        ),
        cutoff,
    )

    assert context.available_before_event.value is AvailabilityValue.UNAVAILABLE
    with pytest.raises(ValueError, match="not cutoff-safe"):
        assert_cutoff_safe(context)


def test_missing_historical_timestamp_is_unknown_and_fails_closed() -> None:
    context = classify_temporal(
        _native_source(
            source_published_at=None,
            provider_observed_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
            first_known_available_at=None,
        ),
        datetime(2025, 1, 29, 21, 0, tzinfo=UTC),
    )

    assert context.available_before_event.value is AvailabilityValue.UNKNOWN
    assert context.temporal_confidence is TemporalConfidence.LOW
    with pytest.raises(ValueError, match="not cutoff-safe"):
        assert_cutoff_safe(context)


def test_manual_context_cannot_claim_availability_against_timestamps() -> None:
    cutoff = datetime(2025, 1, 29, 21, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="contradicts timestamp provenance"):
        TemporalContext(
            source_published_at=datetime(2025, 1, 29, 21, 5, tzinfo=UTC),
            provider_observed_at=datetime(2025, 1, 29, 21, 5, tzinfo=UTC),
            first_known_available_at=datetime(2025, 1, 29, 21, 5, tzinfo=UTC),
            historical_event_cutoff=cutoff,
            available_before_event={
                "value": AvailabilityValue.AVAILABLE,
                "evidence": ("unsupported_manual_claim",),
            },
            reconstruction={"is_reconstructed": False, "reconstructed_at": None},
            temporal_confidence=TemporalConfidence.HIGH,
            fact_truth_status=FactTruthStatus.VERIFIED,
            classification=TemporalClassification.NATIVE,
        )


def test_naive_cutoff_and_incomplete_source_are_rejected() -> None:
    source = _native_source(
        source_published_at=None,
        provider_observed_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        first_known_available_at=None,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        classify_temporal(source, datetime(2025, 1, 29, 21, 0))

    del source["first_known_available_at"]
    with pytest.raises(ValueError, match="explicitly represent first_known_available_at"):
        classify_temporal(source, datetime(2025, 1, 29, 21, 0, tzinfo=UTC))
