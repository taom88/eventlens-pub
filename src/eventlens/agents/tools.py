"""Deterministic, read-only tools used to assemble agent context."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .models import AgentTool, EvidenceItem, EvidenceOrigin


DEFAULT_TOOLS = (
    AgentTool(
        tool_id="read_evidence_passage",
        description="Read an already-supplied, passage-addressable evidence item.",
        read_only=True,
    ),
    AgentTool(
        tool_id="view_research_artifact",
        description="Inspect an already-attributed checksum-addressed research artifact.",
        read_only=True,
    ),
)


def build_transcript_evidence(
    passages: dict[str, str],
    *,
    source_event_date: date,
    source_id: str = "prior-quarter-transcript",
) -> tuple[EvidenceItem, ...]:
    """Normalize transient transcript passages into deterministic agent evidence."""

    return tuple(
        EvidenceItem(
            source_id=source_id,
            passage_id=passage_id,
            origin=EvidenceOrigin.TRANSCRIPT,
            text=text,
            source_event_date=source_event_date,
        )
        for passage_id, text in sorted(passages.items())
    )


def build_vibe_research_evidence(
    *, summary: str, run_id: str, completed_at: datetime
) -> EvidenceItem:
    """Expose a validated Vibe run summary as attributed supporting evidence."""

    return EvidenceItem(
        source_id=f"vibe-trading-run:{run_id}",
        passage_id="vibe-run-summary",
        origin=EvidenceOrigin.VIBE_TRADING,
        text=summary,
        available_at=completed_at,
    )


def build_vibe_bundle_evidence(
    items: list[dict[str, Any]],
    *,
    information_cutoff_at: datetime,
) -> tuple[EvidenceItem, ...]:
    """Convert validated MCP records into passage-addressable agent inputs."""

    evidence = tuple(
        EvidenceItem(
            source_id=item["source_id"],
            passage_id=item["passage_id"],
            origin=EvidenceOrigin.VIBE_TRADING,
            text=item["text"],
            available_at=item["available_at"],
        )
        for item in items
    )
    return tuple(sorted(evidence, key=lambda item: (item.source_id, item.passage_id)))


def build_context_card_evidence(
    cards: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[EvidenceItem, ...]:
    """Expose approved historical-card summaries as citable agent passages."""
    output: list[EvidenceItem] = []
    for card in cards:
        source_id = f"eventlens-context-card:{card['card_id']}"
        event_date = date.fromisoformat(str(card["event_date"]))
        cutoff = datetime.fromisoformat(str(card["knowledge_cutoff_at"]))
        summary = card.get("executive_summary")
        if summary:
            output.append(EvidenceItem(
                source_id=source_id, passage_id="executive-summary",
                origin=EvidenceOrigin.PROVIDER, text=str(summary),
                available_at=cutoff, source_event_date=event_date,
            ))
        for index, message in enumerate(card.get("key_messages", ())):
            if not str(message).strip():
                continue
            output.append(EvidenceItem(
                source_id=source_id, passage_id=f"key-message-{index}",
                origin=EvidenceOrigin.PROVIDER, text=str(message),
                available_at=cutoff, source_event_date=event_date,
            ))
    return tuple(sorted(output, key=lambda item: (item.source_id, item.passage_id)))
