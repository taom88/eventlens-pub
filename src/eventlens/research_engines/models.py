"""Typed, transport-neutral contracts for external research engines.

These models deliberately separate supporting research from EventLens's
canonical financial calculations.  A research engine may provide context and
attributed claims, but it cannot supply calculation fields owned by EventLens.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchStatus(StrEnum):
    """Terminal states reported by a research engine."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ResearchOrigin(StrEnum):
    """Where a result was obtained."""

    LIVE = "live"
    CACHE = "cache"
    FIXTURE = "fixture"
    MOCK = "mock"


class InformationBoundary(StrEnum):
    """The event-data boundary imposed on the upstream research request."""

    PRE_LOCK = "pre_lock"
    POST_LOCK = "post_lock"


class ErrorCode(StrEnum):
    """Transport-neutral, machine-readable research-engine error taxonomy."""

    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UPSTREAM_FAILURE = "upstream_failure"
    MALFORMED_RESPONSE = "malformed_response"
    SCHEMA_VALIDATION = "schema_validation"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ARTIFACT_NOT_FOUND = "artifact_not_found"


class ArtifactReference(BaseModel):
    """A checksum-addressed artifact without embedding its raw contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str = Field(min_length=1, description="Stable opaque artifact reference.")
    media_type: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("reference")
    @classmethod
    def reference_must_be_opaque_and_secret_free(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("artifact reference must not contain credentials, query, or fragment")
        return value


class ResearchWarning(BaseModel):
    """A non-fatal upstream condition that must remain visible downstream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)


class ResearchError(BaseModel):
    """A structured failure that is safe to persist and render."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str = Field(min_length=1)
    retryable: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class Availability(BaseModel):
    """Availability check result, independent of a particular transport."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    transport: str = Field(min_length=1)
    upstream_version: str | None = None
    upstream_commit: str | None = None
    reason: str | None = None


class VibeTradingRunRequest(BaseModel):
    """The complete input contract for an attributed Vibe-Trading run."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: str = Field(default="1.0", min_length=1)
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1, max_length=16)
    event_date: date
    event_release_at: datetime | None = Field(
        default=None,
        description="Known release timestamp required to enforce a pre-lock cutoff.",
    )
    fiscal_quarter: str | None = None
    research_mode: str = Field(min_length=1)
    run_purpose: str = Field(min_length=1)
    question: str = Field(min_length=1)
    allowed_data_sources: tuple[str, ...] = Field(min_length=1)
    allowed_tools: tuple[str, ...] = Field(min_length=1)
    expected_output_format: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    information_boundary: InformationBoundary
    idempotency_key: str = Field(min_length=1)
    audit_correlation_id: str = Field(min_length=1)
    information_cutoff_at: datetime = Field(
        description="Latest data timestamp the engine may use for this request."
    )
    timeout_seconds: float = Field(gt=0, le=3600)
    cancellation_policy: str = Field(min_length=1)

    @field_validator("ticker")
    @classmethod
    def ticker_is_normalized(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.replace(".", "").replace("-", "").isalnum():
            raise ValueError("ticker must contain only letters, digits, dots, or hyphens")
        return normalized

    @field_validator("allowed_data_sources", "allowed_tools")
    @classmethod
    def allowlists_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("allowlist entries must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("allowlist entries must be unique")
        return value

    @field_validator("event_release_at", "information_cutoff_at")
    @classmethod
    def boundary_timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("boundary timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def pre_lock_cutoff_precedes_release(self) -> "VibeTradingRunRequest":
        if self.information_boundary is not InformationBoundary.PRE_LOCK:
            return self
        if self.event_release_at is None:
            raise ValueError("pre-lock research requires event_release_at")
        if self.information_cutoff_at >= self.event_release_at:
            raise ValueError("pre-lock information_cutoff_at must precede event_release_at")
        return self


_PROTECTED_CANONICAL_FIELDS = frozenset(
    {
        "reference_spot",
        "reference_price",
        "reference_close",
        "expected_move",
        "expected_move_pct",
        "implied_move",
        "implied_move_pct",
        "realized_move",
        "realized_move_pct",
        "realized_1d_pct",
        "realized_5d_pct",
        "signed_return_pct",
        "absolute_move_pct",
        "overnight_gap_pct",
        "first_session_intraday_pct",
        "move_gap_pct",
        "realization_ratio",
        "event_window",
        "event_window_selection",
        "volatility_setup",
        "historical_event_study",
        "post_event_comparison",
        "analyst_hypothesis",
        "hypothesis_snapshot",
        "locked_analyst_input",
        "canonical_metrics",
        "eventlens_metrics",
    }
)


class VibeTradingRunResult(BaseModel):
    """Validated result from Vibe-Trading or a deterministic replacement.

    ``structured_payload`` is intentionally supporting research only.  The
    protected keys enforce the boundary that canonical EventLens metrics and
    locked analyst input are never supplied by an upstream research engine.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0", min_length=1)
    status: ResearchStatus
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    audit_correlation_id: str = Field(min_length=1)
    vibe_trading_run_id: str | None = None
    vibe_trading_session_id: str | None = None
    transport: str = Field(min_length=1)
    upstream_version: str | None = None
    upstream_commit: str | None = None
    origin: ResearchOrigin
    summary: str | None = None
    structured_payload: dict[str, Any] | None = None
    artifact_references: tuple[ArtifactReference, ...] = ()
    data_sources: tuple[str, ...] = ()
    tool_trace_references: tuple[ArtifactReference, ...] = ()
    warnings: tuple[ResearchWarning, ...] = ()
    errors: tuple[ResearchError, ...] = ()
    created_at: datetime
    completed_at: datetime
    raw_output_reference: ArtifactReference

    @field_validator("created_at", "completed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_result_state(self) -> "VibeTradingRunResult":
        if self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        if self.status is ResearchStatus.SUCCEEDED and self.errors:
            raise ValueError("a succeeded result cannot contain errors")
        if self.status is ResearchStatus.SUCCEEDED and not self.summary:
            raise ValueError("a succeeded result requires a summary")
        if self.status is ResearchStatus.SUCCEEDED and self.vibe_trading_run_id is None:
            if not any(warning.code == "missing_run_id" for warning in self.warnings):
                raise ValueError("a succeeded result without a run ID requires a missing_run_id warning")
        if self.status in {ResearchStatus.FAILED, ResearchStatus.TIMED_OUT, ResearchStatus.CANCELLED}:
            if not self.errors:
                raise ValueError("a non-success terminal result requires an error")
        if self.status is ResearchStatus.TIMED_OUT and not any(
            error.code is ErrorCode.TIMEOUT for error in self.errors
        ):
            raise ValueError("a timed_out result requires a timeout error")
        if self.status is ResearchStatus.CANCELLED and not any(
            error.code is ErrorCode.CANCELLED for error in self.errors
        ):
            raise ValueError("a cancelled result requires a cancelled error")
        if self.structured_payload is not None:
            protected = _find_protected_fields(self.structured_payload)
            if protected:
                names = ", ".join(sorted(protected))
                raise ValueError(f"research payload contains protected canonical fields: {names}")
        return self


def _find_protected_fields(value: Any) -> set[str]:
    """Return protected keys found anywhere inside untrusted research payload."""

    if isinstance(value, dict):
        found = {
            str(key)
            for key in value
            if str(key).casefold() in _PROTECTED_CANONICAL_FIELDS
        }
        for nested in value.values():
            found.update(_find_protected_fields(nested))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for nested in value:
            found.update(_find_protected_fields(nested))
        return found
    return set()
