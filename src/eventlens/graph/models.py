"""Typed state and messages for the deterministic pre-earnings workflow."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.domain.events import EventTiming, EventWindowResolution
from eventlens.domain.returns import RealizedMoveAnalysis
from eventlens.domain.volatility import (
    ATMStraddleImpliedMove,
    RealizedVolatilityExpectedMoveProxy,
)
from eventlens.committee.packet import DecisionPacket
from eventlens.quant.models import Market
from eventlens.providers.models import ProviderProvenance
from eventlens.research_engines.models import VibeTradingRunResult
from eventlens.config import AgentRoute


class WorkflowStage(StrEnum):
    CREATED = "created"
    AWAITING_DEGRADED_DATA_APPROVAL = "awaiting_degraded_data_approval"
    AWAITING_HYPOTHESIS = "awaiting_hypothesis"
    HYPOTHESIS_LOCKED = "hypothesis_locked"
    AWAITING_EXTERNAL_AGENT = "awaiting_external_agent"
    AWAITING_RISK_REVIEW = "awaiting_risk_review"
    AWAITING_BRIEF_APPROVAL = "awaiting_brief_approval"
    COMPLETED = "completed"


class WorkflowInterrupt(StrEnum):
    ACCEPT_DEGRADED_DATA = "accept_degraded_data"
    LOCK_PRE_EVENT_HYPOTHESIS = "lock_pre_event_hypothesis"
    ACCEPT_RISK_REVIEW = "accept_risk_review"
    APPROVE_PRE_EARNINGS_BRIEF = "approve_pre_earnings_brief"


class HistoricalContextGateStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PASSED = "passed"
    BLOCKED_CONTEXT_MISSING = "blocked_context_missing"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INVALID_CONTEXT = "invalid_context"


class WorkflowRunPurpose(StrEnum):
    """Why a run is being executed; this controls live-data safety policy."""

    LIVE = "live"
    TRIAL = "trial"
    REPLAY = "replay"
    BACKTEST = "backtest"
    FIXTURE = "fixture"


class AnalystHypothesis(BaseModel):
    """Immutable analyst-authored expectation captured before outcomes."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    base_case: str = Field(min_length=1)
    bull_case: str = Field(min_length=1)
    bear_case: str = Field(min_length=1)
    key_debate_points: tuple[str, ...] = Field(min_length=1)
    expected_signed_reaction_pct: Decimal | None = None
    expected_absolute_move_pct: Decimal | None = Field(default=None, ge=0)
    confidence: int = Field(ge=0, le=100)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def expected_reactions_are_consistent(self) -> "AnalystHypothesis":
        if (
            self.expected_signed_reaction_pct is not None
            and self.expected_absolute_move_pct is not None
            and self.expected_absolute_move_pct != abs(self.expected_signed_reaction_pct)
        ):
            raise ValueError(
                "expected_absolute_move_pct must equal abs(expected_signed_reaction_pct)"
            )
        return self


class HistoricalContextReference(BaseModel):
    """Immutable prior-event memory reference available before a target lock."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    card_id: str = Field(min_length=1)
    event_id: str | None = None
    ticker: str = Field(min_length=1)
    event_date: date
    knowledge_cutoff_at: datetime
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executive_summary: str | None = None
    key_messages: tuple[str, ...] = ()
    interpretation: str | None = None

    @field_validator("knowledge_cutoff_at")
    @classmethod
    def cutoff_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("knowledge_cutoff_at must be timezone-aware")
        return value


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    statement: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)


class PreEarningsBrief(BaseModel):
    """Agent prose that references, but cannot overwrite, canonical metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    summary_citations: tuple[EvidenceReference, ...] = Field(min_length=1)
    report_id: str | None = None
    provider: str | None = None
    model: str | None = None
    evidence_claims: tuple[EvidenceClaim, ...] = ()
    caveats: tuple[str, ...] = ()
    follow_up_questions: tuple[str, ...] = ()


class FollowUpAnswer(BaseModel):
    """Ephemeral follow-up output; it is deliberately not workflow state."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    answer: str = Field(min_length=1)
    answer_citations: tuple[EvidenceReference, ...] = Field(min_length=1)
    evidence_claims: tuple[EvidenceClaim, ...] = ()
    caveats: tuple[str, ...] = ()


class WorkflowRequest(BaseModel):
    """Stable inputs used to reproduce a single pre-earnings preparation run."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    session_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    market: Market = Market.US
    exchange_calendar: str = "XNYS"
    event_id: str = Field(min_length=1)
    event_date: date
    event_release_at: datetime
    analyst_confirmed_release_at: datetime | None = None
    confirmed_provider_event_checksum: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    history_start: date
    history_end: date
    post_event_trading_dates: tuple[date, ...] = Field(min_length=5, max_length=10)
    prior_fiscal_quarter: str = Field(min_length=1)
    run_purpose: WorkflowRunPurpose = WorkflowRunPurpose.FIXTURE
    analyst_confirmed_unknown_timing_as: EventTiming | None = None
    historical_unknown_timing_convention: EventTiming | None = None
    accept_degraded_data: bool = False
    idempotency_key: str = Field(min_length=1)
    prior_context_cards: tuple[HistoricalContextReference, ...] = ()

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("event_release_at", "analyst_confirmed_release_at")
    @classmethod
    def release_at_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_release_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_date_window(self) -> "WorkflowRequest":
        expected_calendar = "XHKG" if self.market is Market.HONG_KONG else "XNYS"
        if self.exchange_calendar != expected_calendar:
            raise ValueError(
                f"{self.market.value} workflows require {expected_calendar} exchange calendar"
            )
        if self.history_start > self.history_end:
            raise ValueError("history_start must be on or before history_end")
        if not self.history_start <= self.event_date <= self.history_end:
            raise ValueError("history window must include event_date")
        exchange_timezone = ZoneInfo(
            "Asia/Hong_Kong"
            if self.exchange_calendar == "XHKG"
            else "America/New_York"
        )
        if self.event_release_at.astimezone(exchange_timezone).date() != self.event_date:
            raise ValueError("event_release_at exchange-local date must equal event_date")
        if (
            self.analyst_confirmed_release_at is not None
            and self.analyst_confirmed_release_at.astimezone(exchange_timezone).date()
            != self.event_date
        ):
            raise ValueError(
                "analyst_confirmed_release_at exchange-local date must equal event_date"
            )
        dates = self.post_event_trading_dates
        if any(left >= right for left, right in zip(dates, dates[1:])):
            raise ValueError("post_event_trading_dates must be strictly increasing")
        if any(item < self.event_date for item in dates):
            raise ValueError("post_event_trading_dates cannot precede event_date")
        calendar = xcals.get_calendar(self.exchange_calendar)
        if any(not calendar.is_session(item) for item in dates):
            raise ValueError(
                f"post_event_trading_dates must be {self.exchange_calendar} trading sessions"
            )
        expected_sessions = tuple(
            session.date()
            for session in calendar.sessions_in_range(dates[0], dates[-1])
        )
        if dates != expected_sessions:
            raise ValueError(
                "post_event_trading_dates must be consecutive "
                f"{self.exchange_calendar} trading sessions"
            )
        if self.historical_unknown_timing_convention not in {
            None,
            EventTiming.BEFORE_MARKET_OPEN,
            EventTiming.AFTER_MARKET_CLOSE,
        }:
            raise ValueError("historical unknown timing convention must be BMO or AMC")
        return self


class HistoricalRealizedMove(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    event_date: date
    event_timing: EventTiming | None = None
    reference_close: Decimal | None = Field(default=None, gt=0)
    first_post_open: Decimal | None = Field(default=None, gt=0)
    first_post_close: Decimal | None = Field(default=None, gt=0)
    one_day_signed_move_pct: Decimal | None = None
    one_day_absolute_move_pct: Decimal | None = Field(default=None, ge=0)
    five_day_signed_move_pct: Decimal | None = None
    five_day_absolute_move_pct: Decimal | None = Field(default=None, ge=0)
    overnight_gap_pct: Decimal | None = None
    first_session_intraday_pct: Decimal | None = None
    historical_expected_move_pct: Decimal | None = Field(default=None, ge=0)
    realization_ratio: Decimal | None = Field(default=None, ge=0)
    warnings: tuple[str, ...] = ()


class WorkflowState(BaseModel):
    """Persistable canonical workflow state; raw provider payloads stay out."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    stage: WorkflowStage
    session_id: str
    ticker: str
    market: Market = Market.US
    exchange_calendar: str = "XNYS"
    event_id: str
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_date: date
    # Provider-reported timing is retained separately from any analyst-confirmed
    # convention used to calculate the window.
    event_timing: EventTiming
    effective_event_timing: EventTiming | None = None
    provider_reported_release_at: datetime | None = None
    provider_event_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_event_release_at: datetime
    information_cutoff_at: datetime
    market_data_as_of_date: date | None = None
    prior_fiscal_quarter: str = Field(min_length=1)
    run_purpose: WorkflowRunPurpose = WorkflowRunPurpose.FIXTURE
    prior_transcript_event_date: date
    prior_transcript_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_window: EventWindowResolution | None = None
    provider_provenance: tuple[ProviderProvenance, ...] = ()
    research_result: VibeTradingRunResult | None = None
    expected_move: ATMStraddleImpliedMove | RealizedVolatilityExpectedMoveProxy | None = None
    historical_realized_moves: tuple[HistoricalRealizedMove, ...] = ()
    locked_hypothesis: AnalystHypothesis | None = None
    pre_earnings_brief: PreEarningsBrief | None = None
    decision_packet: DecisionPacket | None = None
    pending_interrupt: WorkflowInterrupt | None = None
    warnings: tuple[str, ...] = ()
    applied_effects: tuple[str, ...] = ()
    prior_context_cards: tuple[HistoricalContextReference, ...] = ()
    historical_context_gate_status: HistoricalContextGateStatus = HistoricalContextGateStatus.NOT_REQUIRED
    historical_context_gate_reason: str | None = None
    historical_context_card_count: int = Field(default=0, ge=0)
    agent_route: AgentRoute | None = None
    external_bundle_id: str | None = None
    external_bundle_revision: int | None = Field(default=None, ge=1)
    external_bundle_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    external_report_id: str | None = None
    external_report_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def state_transition_invariants(self) -> "WorkflowState":
        expected_calendar = "XHKG" if self.market is Market.HONG_KONG else "XNYS"
        if self.exchange_calendar != expected_calendar:
            raise ValueError(
                f"{self.market.value} workflow state requires {expected_calendar}"
            )
        if self.effective_event_release_at <= self.information_cutoff_at:
            raise ValueError("effective release must be strictly after the information cutoff")
        if self.event_window is not None:
            if self.event_window.reported_timing is not self.event_timing:
                raise ValueError("state timing must match the provider-reported event timing")
            if self.effective_event_timing is not self.event_window.effective_timing:
                raise ValueError("state effective timing must match the resolved event window")
        if self.stage is WorkflowStage.AWAITING_DEGRADED_DATA_APPROVAL:
            if self.pending_interrupt is not WorkflowInterrupt.ACCEPT_DEGRADED_DATA:
                raise ValueError("degraded-data stage requires its approval interrupt")
        if self.stage is WorkflowStage.AWAITING_HYPOTHESIS:
            if self.pending_interrupt is not WorkflowInterrupt.LOCK_PRE_EVENT_HYPOTHESIS:
                raise ValueError("hypothesis stage requires hypothesis-lock interrupt")
            if self.locked_hypothesis is not None:
                raise ValueError("hypothesis cannot be locked before its interrupt is resolved")
        if self.stage is WorkflowStage.AWAITING_EXTERNAL_AGENT:
            if self.agent_route is not AgentRoute.EXTERNAL:
                raise ValueError("external-agent stage requires the external route")
            if not (
                self.external_bundle_id
                and self.external_bundle_revision is not None
                and self.external_bundle_sha256
            ):
                raise ValueError("external-agent stage requires an immutable bundle reference")
            if self.external_report_id is not None or self.external_report_sha256 is not None:
                raise ValueError("awaiting external-agent stage cannot retain a validated report")
        if self.stage is WorkflowStage.AWAITING_BRIEF_APPROVAL:
            if (
                self.locked_hypothesis is None
                or self.pre_earnings_brief is None
                or self.decision_packet is None
            ):
                raise ValueError("brief approval requires a locked hypothesis and a brief")
            if self.pending_interrupt is not WorkflowInterrupt.APPROVE_PRE_EARNINGS_BRIEF:
                raise ValueError("brief stage requires brief-approval interrupt")
        if self.stage is WorkflowStage.AWAITING_RISK_REVIEW:
            if self.decision_packet is None:
                raise ValueError("risk review requires a retained decision packet")
            if self.pending_interrupt is not WorkflowInterrupt.ACCEPT_RISK_REVIEW:
                raise ValueError("risk-review stage requires its analyst interrupt")
        if self.stage is WorkflowStage.COMPLETED:
            if (
                self.locked_hypothesis is None
                or self.pre_earnings_brief is None
                or self.decision_packet is None
            ):
                raise ValueError(
                    "completed pre-earnings workflow requires hypothesis, brief, and decision packet"
                )
            if self.pending_interrupt is not None:
                raise ValueError("completed workflow cannot retain a pending interrupt")
        return self
