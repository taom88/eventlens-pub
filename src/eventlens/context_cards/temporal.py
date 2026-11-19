"""Fail-closed temporal classification for EventLens v4 observations.

Truth and historical availability are deliberately separate dimensions here.
The classifier answers only whether an observation is proven to have been
available before a declared cutoff; it never promotes or demotes the truth
status supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_AWARE_DATETIME = TypeAdapter(AwareDatetime)
_MISSING = object()


class FactTruthStatus(StrEnum):
    """Factual assessment, independent of when the fact was available."""

    VERIFIED = "verified"
    REPORTED = "reported"
    UNVERIFIED = "unverified"
    REFUTED = "refuted"
    CONFLICTED = "conflicted"
    UNKNOWN = "unknown"


class AvailabilityValue(StrEnum):
    """Three-valued answer to ``available before the cutoff?``"""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class TemporalConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TemporalClassification(StrEnum):
    """Whether the historical observation is native or reconstructed later."""

    NATIVE = "native"
    RECONSTRUCTED = "reconstructed"


class _TemporalModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class _AvailabilityAssessment(_TemporalModel):
    value: AvailabilityValue
    evidence: tuple[NonEmptyStr, ...] = Field(min_length=1)


class _ReconstructionState(_TemporalModel):
    is_reconstructed: bool
    reconstructed_at: AwareDatetime | None

    @model_validator(mode="after")
    def timestamp_matches_state(self) -> "_ReconstructionState":
        if self.is_reconstructed and self.reconstructed_at is None:
            raise ValueError("reconstructed evidence requires reconstructed_at")
        if not self.is_reconstructed and self.reconstructed_at is not None:
            raise ValueError("native evidence cannot carry reconstructed_at")
        return self


class TemporalContext(_TemporalModel):
    """Complete temporal provenance for one external observation."""

    source_published_at: AwareDatetime | None
    provider_observed_at: AwareDatetime
    first_known_available_at: AwareDatetime | None
    historical_event_cutoff: AwareDatetime
    available_before_event: _AvailabilityAssessment
    reconstruction: _ReconstructionState
    temporal_confidence: TemporalConfidence
    fact_truth_status: FactTruthStatus
    classification: TemporalClassification

    @property
    def availability_value(self) -> AvailabilityValue:
        return self.available_before_event.value

    @property
    def availability_evidence(self) -> tuple[str, ...]:
        return self.available_before_event.evidence

    @property
    def is_reconstructed(self) -> bool:
        return self.reconstruction.is_reconstructed

    @property
    def reconstructed_at(self) -> datetime | None:
        return self.reconstruction.reconstructed_at

    @model_validator(mode="after")
    def temporal_fields_are_coherent(self) -> "TemporalContext":
        expected_classification = (
            TemporalClassification.RECONSTRUCTED
            if self.reconstruction.is_reconstructed
            else TemporalClassification.NATIVE
        )
        if self.classification is not expected_classification:
            raise ValueError("classification must match reconstruction state")

        if (
            self.source_published_at is not None
            and self.first_known_available_at is not None
            and self.first_known_available_at < self.source_published_at
        ):
            raise ValueError("first_known_available_at cannot precede source_published_at")
        if (
            self.source_published_at is not None
            and self.provider_observed_at < self.source_published_at
        ):
            raise ValueError("provider_observed_at cannot precede source_published_at")
        if (
            self.reconstructed_at is not None
            and self.reconstructed_at > self.provider_observed_at
        ):
            raise ValueError("reconstructed_at cannot follow provider_observed_at")

        expected_value, expected_confidence, _ = _classify_values(
            source_published_at=self.source_published_at,
            provider_observed_at=self.provider_observed_at,
            first_known_available_at=self.first_known_available_at,
            target_cutoff=self.historical_event_cutoff,
            is_reconstructed=self.reconstruction.is_reconstructed,
        )
        if self.available_before_event.value is not expected_value:
            raise ValueError("availability value contradicts timestamp provenance")
        if self.temporal_confidence is not expected_confidence:
            raise ValueError("temporal confidence contradicts timestamp provenance")
        return self


def classify_temporal(source: object, target_cutoff: datetime | str) -> TemporalContext:
    """Classify one observation relative to an explicit historical cutoff.

    ``source`` may be an object or mapping, but it must explicitly provide the
    publication, provider-observation, first-known-availability, reconstruction,
    and truth fields. Nullable timestamps must still be represented with
    ``None`` so missing provenance cannot be confused with omitted provenance.
    """

    cutoff = _as_aware_datetime(target_cutoff, field_name="target_cutoff")
    source_published_at = _optional_source_datetime(source, "source_published_at")
    provider_observed_at = _required_source_datetime(source, "provider_observed_at")
    first_known_available_at = _optional_source_datetime(
        source, "first_known_available_at"
    )
    is_reconstructed, reconstructed_at = _read_reconstruction(source)
    truth_status = FactTruthStatus(_required_source_value(source, "fact_truth_status"))

    value, confidence, reason = _classify_values(
        source_published_at=source_published_at,
        provider_observed_at=provider_observed_at,
        first_known_available_at=first_known_available_at,
        target_cutoff=cutoff,
        is_reconstructed=is_reconstructed,
    )
    supplied_evidence = _source_value(source, "availability_evidence", default=())
    if isinstance(supplied_evidence, str):
        supplied_evidence = (supplied_evidence,)
    evidence = tuple(supplied_evidence) + (reason,)

    return TemporalContext(
        source_published_at=source_published_at,
        provider_observed_at=provider_observed_at,
        first_known_available_at=first_known_available_at,
        historical_event_cutoff=cutoff,
        available_before_event={"value": value, "evidence": evidence},
        reconstruction={
            "is_reconstructed": is_reconstructed,
            "reconstructed_at": reconstructed_at,
        },
        temporal_confidence=confidence,
        fact_truth_status=truth_status,
        classification=(
            TemporalClassification.RECONSTRUCTED
            if is_reconstructed
            else TemporalClassification.NATIVE
        ),
    )


def assert_cutoff_safe(context: TemporalContext) -> TemporalContext:
    """Return ``context`` only when it is safe for a pre-event backtest.

    Unknown availability, later reconstruction, or anything short of a
    high-confidence native timestamp chain fails closed.
    """

    if not isinstance(context, TemporalContext):
        raise TypeError("assert_cutoff_safe requires a TemporalContext")
    blockers: list[str] = []
    if context.available_before_event.value is not AvailabilityValue.AVAILABLE:
        blockers.append(f"availability={context.available_before_event.value.value}")
    if context.classification is not TemporalClassification.NATIVE:
        blockers.append(f"classification={context.classification.value}")
    if context.temporal_confidence is not TemporalConfidence.HIGH:
        blockers.append(f"temporal_confidence={context.temporal_confidence.value}")
    if blockers:
        raise ValueError("evidence is not cutoff-safe: " + ", ".join(blockers))
    return context


def _classify_values(
    *,
    source_published_at: datetime | None,
    provider_observed_at: datetime,
    first_known_available_at: datetime | None,
    target_cutoff: datetime,
    is_reconstructed: bool,
) -> tuple[AvailabilityValue, TemporalConfidence, str]:
    if first_known_available_at is not None:
        value = (
            AvailabilityValue.AVAILABLE
            if first_known_available_at < target_cutoff
            else AvailabilityValue.UNAVAILABLE
        )
        confidence = (
            TemporalConfidence.LOW if is_reconstructed else TemporalConfidence.HIGH
        )
        relation = "before" if value is AvailabilityValue.AVAILABLE else "at_or_after"
        return (
            value,
            confidence,
            f"first_known_available_at_{relation}_historical_event_cutoff",
        )

    if source_published_at is not None:
        value = (
            AvailabilityValue.AVAILABLE
            if source_published_at < target_cutoff
            else AvailabilityValue.UNAVAILABLE
        )
        confidence = (
            TemporalConfidence.LOW if is_reconstructed else TemporalConfidence.HIGH
        )
        relation = "before" if value is AvailabilityValue.AVAILABLE else "at_or_after"
        return (
            value,
            confidence,
            f"source_published_at_{relation}_historical_event_cutoff",
        )

    if not is_reconstructed and provider_observed_at < target_cutoff:
        return (
            AvailabilityValue.AVAILABLE,
            TemporalConfidence.MEDIUM,
            "provider_observed_at_before_cutoff_without_publication_timestamp",
        )

    return (
        AvailabilityValue.UNKNOWN,
        TemporalConfidence.LOW,
        "no_timestamp_proves_availability_before_historical_event_cutoff",
    )


def _read_reconstruction(source: object) -> tuple[bool, datetime | None]:
    nested = _source_value(source, "reconstruction", default=_MISSING)
    if nested is not _MISSING:
        is_reconstructed = _required_source_value(nested, "is_reconstructed")
        reconstructed_at = _optional_source_datetime(nested, "reconstructed_at")
    else:
        is_reconstructed = _required_source_value(source, "is_reconstructed")
        reconstructed_at = _optional_source_datetime(source, "reconstructed_at")
    if not isinstance(is_reconstructed, bool):
        raise TypeError("is_reconstructed must be a boolean")
    if is_reconstructed and reconstructed_at is None:
        raise ValueError("reconstructed evidence requires reconstructed_at")
    if not is_reconstructed and reconstructed_at is not None:
        raise ValueError("native evidence cannot carry reconstructed_at")
    return is_reconstructed, reconstructed_at


def _required_source_datetime(source: object, field_name: str) -> datetime:
    value = _required_source_value(source, field_name)
    if value is None:
        raise ValueError(f"{field_name} cannot be null")
    return _as_aware_datetime(value, field_name=field_name)


def _optional_source_datetime(source: object, field_name: str) -> datetime | None:
    value = _required_source_value(source, field_name)
    if value is None:
        return None
    return _as_aware_datetime(value, field_name=field_name)


def _as_aware_datetime(value: object, *, field_name: str) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        raise ValueError(f"{field_name} must include an exact timezone-aware time")
    if isinstance(value, str) and ("T" not in value or not _has_timezone_suffix(value)):
        raise ValueError(f"{field_name} must include an exact timezone-aware time")
    try:
        return _AWARE_DATETIME.validate_python(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a timezone-aware datetime") from exc


def _has_timezone_suffix(value: str) -> bool:
    time_part = value.rsplit("T", 1)[-1]
    return value.endswith(("Z", "z")) or "+" in time_part or "-" in time_part


def _required_source_value(source: object, field_name: str) -> Any:
    value = _source_value(source, field_name, default=_MISSING)
    if value is _MISSING:
        raise ValueError(f"source must explicitly represent {field_name}")
    return value


def _source_value(source: object, field_name: str, *, default: object) -> Any:
    if isinstance(source, Mapping):
        return source.get(field_name, default)
    return getattr(source, field_name, default)


__all__ = [
    "FactTruthStatus",
    "AvailabilityValue",
    "TemporalConfidence",
    "TemporalClassification",
    "TemporalContext",
    "classify_temporal",
    "assert_cutoff_safe",
]
