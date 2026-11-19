"""Typed, evidence-grounded contracts for EventLens interpretation agents."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceOrigin(StrEnum):
    COMPANY = "company"
    TRANSCRIPT = "transcript"
    VIBE_TRADING = "vibe_trading"
    ANALYST = "analyst"
    PROVIDER = "provider"


class ClaimKind(StrEnum):
    FACT = "fact"
    INTERPRETATION = "interpretation"


class ReportKind(StrEnum):
    PRE_EARNINGS_BRIEF = "pre_earnings_brief"
    POST_EARNINGS_REVIEW = "post_earnings_review"
    FOLLOW_UP = "follow_up"


class AgentErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    TRANSPORT = "transport"
    AUTHENTICATION = "authentication"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_EVIDENCE = "invalid_evidence"
    POLICY_VIOLATION = "policy_violation"


class EvidenceItem(BaseModel):
    """A passage-level input the agent may cite, not an agent-created source."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)
    origin: EvidenceOrigin
    text: str = Field(min_length=1)
    available_at: datetime | None = None
    source_event_date: date | None = None

    @field_validator("available_at")
    @classmethod
    def available_at_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def has_nonfabricated_temporal_context(self) -> "EvidenceItem":
        if self.available_at is None and self.source_event_date is None:
            raise ValueError("evidence requires available_at or source_event_date")
        return self


class Citation(BaseModel):
    """A report claim's immutable reference to a supplied evidence passage."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)


class EvidenceClaim(BaseModel):
    """One rendered statement, with citations required for both facts and interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: ClaimKind
    text: str = Field(min_length=1, max_length=4_000)
    citations: tuple[Citation, ...] = Field(min_length=1)
    proposition_id: str | None = None
    topic_id: str | None = None
    stance: str | None = None
    assumption_ids: tuple[str, ...] = ()
    missing_data_gap_ids: tuple[str, ...] = ()
    timing_sensitive: bool = False
    supported: bool = True

    @field_validator("text")
    @classmethod
    def prohibits_canonical_metric_or_advice_mutation(cls, value: str) -> str:
        return _safe_interpretation_text(value)


class AgentReportDraft(BaseModel):
    """The JSON shape a model must return; EventLens assigns its durable ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    headline: str = Field(min_length=1, max_length=500)
    executive_summary: str = Field(min_length=1, max_length=4_000)
    executive_summary_citations: tuple[Citation, ...] = Field(min_length=1)
    claims: tuple[EvidenceClaim, ...] = Field(min_length=1, max_length=24)
    caveats: tuple[str, ...] = Field(default=(), max_length=16)
    follow_up_questions: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("headline", "executive_summary")
    @classmethod
    def report_text_respects_supporting_research_boundary(cls, value: str) -> str:
        return _safe_interpretation_text(value)

    @field_validator("caveats", "follow_up_questions")
    @classmethod
    def report_text_lists_respect_supporting_research_boundary(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return tuple(_safe_interpretation_text(item) for item in value)


class AnalystReport(AgentReportDraft):
    """EventLens-owned immutable interpretation output for a report-first workflow."""

    report_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    kind: ReportKind
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class AgentSkill(BaseModel):
    """A named, prompt-level capability; it is never executable code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=1)


class AgentTool(BaseModel):
    """A graph-owned tool contract, declared here so prompts can be allowlisted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=1)
    read_only: bool = True


class AnalystInterpretationRequest(BaseModel):
    """Stable agent input; it only accepts supporting evidence and tool IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: str = Field(default="1.0", min_length=1)
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1, max_length=16)
    kind: ReportKind
    question: str = Field(min_length=1, max_length=4_000)
    prompt_version: str = Field(min_length=1)
    allowed_skill_ids: tuple[str, ...] = Field(min_length=1)
    allowed_tool_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceItem, ...] = Field(min_length=1, max_length=128)
    prior_report_id: str | None = None
    follow_up_question: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.replace(".", "").replace("-", "").isalnum():
            raise ValueError("ticker must contain only letters, digits, dots, or hyphens")
        return normalized

    @field_validator("allowed_skill_ids", "allowed_tool_ids")
    @classmethod
    def allowlists_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowlisted IDs must be unique")
        return value

    @model_validator(mode="after")
    def follow_up_is_report_first(self) -> "AnalystInterpretationRequest":
        if self.kind is ReportKind.FOLLOW_UP:
            if not self.prior_report_id or not self.follow_up_question:
                raise ValueError("follow-up interpretation requires prior_report_id and follow_up_question")
        elif self.prior_report_id is not None or self.follow_up_question is not None:
            raise ValueError("prior report fields are only valid for follow-up interpretation")
        return self


class AgentFailure(BaseModel):
    """Secret-safe provider failure suitable for workflow state and audit use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: AgentErrorCode
    message: str = Field(min_length=1)
    retryable: bool = False


class AgentReasoningResult(BaseModel):
    """Typed success/failure envelope; adapters never expose raw completions or keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    attempts: int = Field(ge=0)
    report: AnalystReport | None = None
    errors: tuple[AgentFailure, ...] = ()

    @model_validator(mode="after")
    def has_exactly_one_terminal_shape(self) -> "AgentReasoningResult":
        if (self.report is None) == (not self.errors):
            raise ValueError("reasoning result requires either one report or one or more errors")
        return self


def validate_report_evidence(
    report: AgentReportDraft, request: AnalystInterpretationRequest
) -> None:
    """Reject claims whose citations do not identify a supplied evidence passage."""

    valid_references = {(item.source_id, item.passage_id) for item in request.evidence}
    for citation in report.executive_summary_citations:
        if (citation.source_id, citation.passage_id) not in valid_references:
            raise ValueError("executive summary citation does not reference supplied evidence")
    for claim in report.claims:
        for citation in claim.citations:
            if (citation.source_id, citation.passage_id) not in valid_references:
                raise ValueError("claim citation does not reference supplied evidence")


def _safe_interpretation_text(value: str) -> str:
    lowered = value.casefold()
    protected = (
        "reference spot",
        "reference close",
        "expected move",
        "implied move",
        "realized move",
        "realization ratio",
        "event window selection",
    )
    advice = (
        "buy ",
        "purchase ",
        "sell ",
        "short ",
        "go long",
        "go short",
        "trade ",
        "accumulate ",
        "initiate ",
        "enter a position",
        "recommend owning",
        "recommend buying",
        "recommend selling",
        "should own",
        "should buy",
        "should sell",
        "overweight ",
        "underweight ",
        "owning calls",
        "own calls",
        "own puts",
    )
    if any(term in lowered for term in protected):
        raise ValueError("agent reports cannot supply canonical EventLens metrics")
    if any(term in lowered for term in advice):
        raise ValueError("agent reports cannot provide trading or execution advice")
    return value
