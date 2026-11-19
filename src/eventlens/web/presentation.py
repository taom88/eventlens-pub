"""UI-neutral, secret-safe presentation models shared by UI and tests.

The graph state remains the canonical domain contract.  This module contains
only read-side shaping: it does not calculate reactions, resolve events, or
perform provider calls.  Keeping this boundary typed means a renderer can be
changed without changing workflow semantics.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eventlens.config import EventLensSettings
from eventlens.graph.models import (
    HistoricalContextGateStatus,
    WorkflowStage,
    WorkflowState,
)
from eventlens.providers.models import DataQualityStatus
from eventlens.web.artifact_trace import ArtifactTraceReader, ArtifactTraceSpec, TraceStatus


_SECRET_KEYS = re.compile(r"(api[_-]?key|secret|token|authorization|password)", re.I)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*")
_SECRET_ASSIGNMENT = re.compile(
    r'''(?ix)(["']?(?:api[_-]?key|secret|token|authorization|password)["']?\s*[:=]\s*)
    (?:["'][^"']*["']|[^\s,;&]+)'''
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|secret|password)=)[^&#\s]+"
)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if _SECRET_KEYS.search(str(key)) else redact_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def citation_label(source_id: str, passage_id: str) -> str:
    return f"[{source_id} · {passage_id}]"


class SessionEventView(BaseModel):
    """The event identity and timing values safe to display to an analyst."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    event_id: str
    event_date: date
    reported_timing: str
    effective_timing: str | None = None
    provider_release_at: datetime | None = None
    effective_release_at: datetime | None = None


class PriorCardCoverage(BaseModel):
    """Coverage summary without exposing raw card payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: int = Field(ge=0)
    preferred: int = Field(default=3, ge=0)
    required: int = Field(default=3, ge=0)
    card_ids: tuple[str, ...] = ()
    cards: tuple["PriorCardView", ...] = ()
    status: str
    reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.available >= self.required


class PriorCardView(BaseModel):
    """Readable prior-card detail reserved for the context-card view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str
    event_date: date
    executive_summary: str | None = None
    key_messages: tuple[str, ...] = ()
    interpretation: str | None = None
    content_sha256: str


class FutureStageKey(StrEnum):
    """UI pipeline stages beyond the legacy graph's terminal state."""

    ONTOLOGY = "company_context_ontology"
    SEMANTIC_NEWS = "content_aware_semantic_news"
    VALUATION = "valuation_basis"
    RECOMMENDATION = "refined_recommendation_synthesis"
    COMMITTEE = "refined_committee_risk_adjudication"
    QA = "qa_release_gate"
    POST_EVENT = "post_event_outcome"


class FutureStageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: FutureStageKey
    label: str
    available: bool = False
    reason: str


class SessionStageView(BaseModel):
    """UI status independent from the graph's legacy terminal enum."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_stage: WorkflowStage | None
    label: str
    workflow_terminal: bool = False
    semantic_completion: bool = False


class ArtifactLink(BaseModel):
    """A safe, inspectable pointer to a persisted artifact.

    Paths are metadata only.  The presentation layer never reads or writes the
    pointed-to file; ``available`` makes missing artifacts explicit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    label: str
    path: str
    available: bool = False
    checksum: str | None = None


class SessionView(BaseModel):
    """Single typed read model consumed by the Streamlit shell and tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str | None = None
    run_mode: str | None = None
    stage: WorkflowStage | None = None
    stage_status: SessionStageView = SessionStageView(
        workflow_stage=None, label="No session loaded"
    )
    event: SessionEventView | None = None
    cutoff_at: datetime | None = None
    provider_origin: str | None = None
    provider_origins: tuple[str, ...] = ()
    prior_card_coverage: PriorCardCoverage = PriorCardCoverage(
        available=0, status="not_loaded"
    )
    persistence_backend: str | None = None
    artifact_links: tuple[ArtifactLink, ...] = ()
    warnings: tuple[str, ...] = ()
    missing_future_stages: tuple[WorkflowStage, ...] = ()
    unavailable_future_stages: tuple[FutureStageView, ...] = ()
    pending_interrupt: str | None = None
    degraded: bool = False
    awaiting_agent: bool = False
    error: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @property
    def event_id(self) -> str | None:
        return self.event.event_id if self.event else None

    @property
    def ticker(self) -> str | None:
        return self.event.ticker if self.event else None


_STAGE_ORDER: tuple[WorkflowStage, ...] = (
    WorkflowStage.CREATED,
    WorkflowStage.AWAITING_DEGRADED_DATA_APPROVAL,
    WorkflowStage.AWAITING_HYPOTHESIS,
    WorkflowStage.HYPOTHESIS_LOCKED,
    WorkflowStage.AWAITING_EXTERNAL_AGENT,
    WorkflowStage.AWAITING_RISK_REVIEW,
    WorkflowStage.AWAITING_BRIEF_APPROVAL,
    WorkflowStage.COMPLETED,
)


def session_view(
    state: WorkflowState | None,
    settings: EventLensSettings | None = None,
    *,
    error: str | None = None,
    persistence_backend: str | None = None,
) -> SessionView:
    """Build a read model from already-validated state.

    This function deliberately accepts no provider or renderer objects.  A
    missing state is represented as an empty/error view rather than guessed
    domain values, which keeps degraded and failure states visible.
    """

    if state is not None and not isinstance(state, WorkflowState):
        raise TypeError("session_view requires a validated WorkflowState or None")
    backend = persistence_backend or (settings.memory_backend if settings else None)
    if state is None:
        return SessionView(
            persistence_backend=backend,
            missing_future_stages=_STAGE_ORDER,
            unavailable_future_stages=_unavailable_future_stages(),
            error=error,
        )

    origins = tuple(
        sorted({item.origin.value for item in state.provider_provenance})
    )
    # Run purpose is not provider provenance.  Never label a missing provider
    # receipt as live or fixture merely because the workflow was configured for
    # that purpose.
    origin = origins[0] if len(origins) == 1 else ("mixed" if origins else None)

    gate = state.historical_context_gate_status
    card_count = max(
        len(state.prior_context_cards), state.historical_context_card_count
    )
    required = 3
    if gate is HistoricalContextGateStatus.NOT_REQUIRED and not state.prior_context_cards:
        coverage_status = "not_required"
        reason = state.historical_context_gate_reason
    elif gate is HistoricalContextGateStatus.PASSED:
        coverage_status = "passed"
        reason = state.historical_context_gate_reason
    elif gate.value in {"blocked_context_missing", "insufficient_history", "invalid_context"}:
        coverage_status = gate.value
        reason = state.historical_context_gate_reason
    else:
        coverage_status = "available" if card_count else "missing"
        reason = state.historical_context_gate_reason
    coverage = PriorCardCoverage(
        available=card_count,
        required=required,
        preferred=3,
        card_ids=tuple(item.card_id for item in state.prior_context_cards),
        cards=tuple(
            PriorCardView(
                card_id=item.card_id,
                event_date=item.event_date,
                executive_summary=item.executive_summary,
                key_messages=item.key_messages,
                interpretation=item.interpretation,
                content_sha256=item.content_sha256,
            )
            for item in state.prior_context_cards
        ),
        status=coverage_status,
        reason=reason,
    )

    warnings = list(state.warnings)
    for item in state.provider_provenance:
        warnings.extend(f"{item.provider_name}: {warning.message}" for warning in item.warnings)
    # Stable de-duplication avoids repeating a warning when it is retained in
    # both the workflow and provider provenance portions of state.
    warning_values = tuple(dict.fromkeys(item for item in warnings if item))
    degraded = (
        state.stage is WorkflowStage.AWAITING_DEGRADED_DATA_APPROVAL
        or any(
            item.data_quality
            in {
                DataQualityStatus.PARTIAL,
                DataQualityStatus.DEGRADED,
                DataQualityStatus.UNAVAILABLE,
            }
            for item in state.provider_provenance
        )
    )
    links = _artifact_links(state, settings.workflow_artifact_dir if settings else None)
    return SessionView(
        session_id=state.session_id,
        run_mode=state.run_purpose.value,
        stage=state.stage,
        stage_status=_stage_status(state.stage),
        event=SessionEventView(
            ticker=state.ticker,
            event_id=state.event_id,
            event_date=state.event_date,
            reported_timing=state.event_timing.value,
            effective_timing=(
                state.effective_event_timing.value
                if state.effective_event_timing is not None
                else None
            ),
            provider_release_at=state.provider_reported_release_at,
            effective_release_at=state.effective_event_release_at,
        ),
        cutoff_at=state.information_cutoff_at,
        provider_origin=origin,
        provider_origins=origins,
        prior_card_coverage=coverage,
        persistence_backend=backend,
        artifact_links=links,
        warnings=warning_values,
        missing_future_stages=_future_stages(state.stage),
        unavailable_future_stages=_unavailable_future_stages(),
        pending_interrupt=(
            state.pending_interrupt.value if state.pending_interrupt is not None else None
        ),
        degraded=degraded,
        awaiting_agent=state.stage is WorkflowStage.AWAITING_EXTERNAL_AGENT,
        error=error,
    )


def product_session_view(
    session: "ProductSession",
    settings: EventLensSettings | None = None,
) -> SessionView:
    """Project the product façade onto the same session read model as the graph.

    Product artifacts remain on ``ProductSession`` for bounded downloads, but
    every session-level status rendered by Streamlit comes through this model.
    The function performs no provider calls and does not read artifact bodies.
    """

    from eventlens.web.product_session import ProductSession

    if not isinstance(session, ProductSession):
        raise TypeError("product_session_view requires a validated ProductSession")
    try:
        workflow_stage = WorkflowStage(session.stage)
    except ValueError:
        workflow_stage = None
    origins = (session.origin,) if session.origin else ()
    event = None
    if session.ticker and session.event_id and session.event_date:
        event = SessionEventView(
            ticker=session.ticker,
            event_id=session.event_id,
            event_date=session.event_date,
            reported_timing="provider_or_assumed",
            provider_release_at=session.event_release_at,
            effective_release_at=session.event_release_at,
        )
    coverage = PriorCardCoverage(
        available=len(session.context_refs),
        preferred=3,
        required=3,
        card_ids=tuple(reference.path.stem for reference in session.context_refs),
        status=("passed" if len(session.context_refs) >= 3 else "insufficient_history"),
        reason=(None if len(session.context_refs) >= 3 else "three prior cards are required"),
    )
    links = tuple(
        ArtifactLink(
            kind=reference.kind,
            label=reference.kind.replace("_", " ").title(),
            path=reference.path.name,
            available=reference.verified,
            checksum=reference.sha256,
        )
        for reference in session.artifact_refs
    )
    return SessionView(
        session_id=session.session_id,
        run_mode=session.mode.value,
        stage=workflow_stage,
        stage_status=SessionStageView(
            workflow_stage=workflow_stage,
            label=session.stage.replace("_", " ").title(),
            workflow_terminal=session.status == "validated",
            semantic_completion=False,
        ),
        event=event,
        cutoff_at=session.information_cutoff_at,
        provider_origin=session.origin,
        provider_origins=origins,
        prior_card_coverage=coverage,
        persistence_backend="local_product_store",
        artifact_links=links,
        warnings=session.warnings,
        unavailable_future_stages=_unavailable_future_stages(),
        awaiting_agent=session.status == "awaiting_external_agent",
        error=session.error.message if session.error is not None else None,
    )


def error_session_view(
    error: BaseException | str,
    settings: EventLensSettings | None = None,
) -> SessionView:
    """Return a secret-safe error view for a failed load/render operation."""

    if isinstance(error, BaseException):
        # Unknown exception strings can echo provider response bodies, URLs,
        # local paths, or private prompts. Typed application errors should be
        # converted to a public string before reaching this generic boundary.
        message = f"{type(error).__name__} while loading the research session"
    else:
        message = _redact_text(str(error).strip() or "Research session error")
    return session_view(None, settings, error=message[:500])


def _redact_text(value: str) -> str:
    text = _BEARER.sub("Bearer [REDACTED]", value)
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    text = _QUERY_SECRET.sub(r"\1[REDACTED]", text)
    lines = []
    for line in text.splitlines() or [text]:
        if re.search(r"(?i)\b(?:raw[_ -]?prompt|private[_ -]?prompt|system[_ -]?message)\s*[:=]", line):
            lines.append("[REDACTED PRIVATE INPUT]")
        else:
            lines.append(line)
    return "\n".join(lines)


def _future_stages(stage: WorkflowStage) -> tuple[WorkflowStage, ...]:
    try:
        position = _STAGE_ORDER.index(stage)
    except ValueError:
        return _STAGE_ORDER
    return _STAGE_ORDER[position + 1 :]


def _unavailable_future_stages() -> tuple[FutureStageView, ...]:
    return tuple(
        FutureStageView(
            key=key,
            label=label,
            reason="not implemented in the D0 product slice",
        )
        for key, label in (
            (FutureStageKey.ONTOLOGY, "Company context ontology"),
            (FutureStageKey.SEMANTIC_NEWS, "Content-aware semantic news signal"),
            (FutureStageKey.VALUATION, "Valuation basis"),
            (FutureStageKey.RECOMMENDATION, "Refined recommendation synthesis"),
            (FutureStageKey.COMMITTEE, "Refined committee, risk, and adjudication"),
            (FutureStageKey.QA, "Independent QA release gate"),
            (FutureStageKey.POST_EVENT, "Post-event outcome and scoring"),
        )
    )


def _stage_status(stage: WorkflowStage) -> SessionStageView:
    labels = {
        WorkflowStage.CREATED: "Session created",
        WorkflowStage.AWAITING_DEGRADED_DATA_APPROVAL: "Degraded data approval pending",
        WorkflowStage.AWAITING_HYPOTHESIS: "Hypothesis lock pending",
        WorkflowStage.HYPOTHESIS_LOCKED: "Hypothesis locked",
        WorkflowStage.AWAITING_EXTERNAL_AGENT: "External-agent report pending",
        WorkflowStage.AWAITING_RISK_REVIEW: "Risk review pending",
        WorkflowStage.AWAITING_BRIEF_APPROVAL: "Brief approval pending",
        WorkflowStage.COMPLETED: "Workflow checkpoint complete",
    }
    return SessionStageView(
        workflow_stage=stage,
        label=labels[stage],
        workflow_terminal=stage is WorkflowStage.COMPLETED,
        # Ontology, valuation, QA, and post-event status remain unavailable;
        # a legacy graph checkpoint cannot claim semantic product completion.
        semantic_completion=False,
    )


def _artifact_links(state: WorkflowState, root: Path | None) -> tuple[ArtifactLink, ...]:
    if root is None:
        return ()
    # This mirrors the durable report writer's path shape, but does not create
    # artifacts as a side effect of rendering a read model.
    safe_session = _safe_segment(state.session_id)
    safe_event = _safe_segment(state.event_id)
    directory = root.resolve() / safe_session / safe_event
    reader = ArtifactTraceReader(root)
    links: list[ArtifactLink] = []
    for kind, label, filename, checksum in (
        ("decision_packet_json", "Decision packet JSON", "decision-packet.json", None),
        ("decision_packet_markdown", "Decision packet Markdown", "decision-packet.md", None),
    ):
        path = directory / filename
        record = reader.read(
            ArtifactTraceSpec(
                path=path,
                artifact_id=f"{safe_session}:{safe_event}:{kind}",
                artifact_type=kind,
                expected_sha256=checksum,
            )
        )
        links.append(
            ArtifactLink(
                kind=kind,
                label=label,
                path=record.path,
                available=record.status is TraceStatus.AVAILABLE,
                checksum=record.checksum,
            )
        )
    return tuple(links)


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:120] or "unknown"


# Stable descriptive aliases for callers that use the ticket terminology.
SessionReadModel = SessionView
build_session_view = session_view
