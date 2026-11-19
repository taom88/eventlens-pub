"""Repository aggregate/link types built on the canonical persistence records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from eventlens.persistence.memory_models import *  # noqa: F403


def canonical_sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LinkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class EventSourceLink(LinkModel):
    source_id: UUID
    relationship: str = Field(min_length=1)
    is_primary: bool = False


class EvidenceLink(LinkModel):
    source_id: UUID
    passage_id: str = Field(min_length=1)
    relationship: str = Field(min_length=1)
    available_at: AwareDatetime | None = None
    inclusion_reason: str | None = None


class EventMemoryBundle(LinkModel):
    company: CompanyRecord  # noqa: F405
    event: EarningsEventRecord  # noqa: F405
    metrics: tuple[EventMetricRecord, ...] = ()  # noqa: F405
    market_setup: MarketSetupRecord | None = None  # noqa: F405
    market_reaction: MarketReactionRecord | None = None  # noqa: F405
    sources: tuple[SourceRecord, ...] = ()  # noqa: F405
    source_passages: tuple[SourcePassageRecord, ...] = ()  # noqa: F405
    session: ResearchSessionRecord  # noqa: F405
    hypotheses: tuple[HypothesisRecord, ...] = ()  # noqa: F405
    hypothesis_assessments: tuple[HypothesisAssessmentRecord, ...] = ()  # noqa: F405
    snapshots: tuple[PreEventSnapshotRecord, ...] = ()  # noqa: F405
    context_cards: tuple[EventContextCardRecord, ...] = ()  # noqa: F405
    event_source_links: tuple[EventSourceLink, ...] = ()
    snapshot_evidence: dict[UUID, tuple[EvidenceLink, ...]] = Field(default_factory=dict)
    assessment_evidence: dict[UUID, tuple[EvidenceLink, ...]] = Field(default_factory=dict)
    context_card_evidence: dict[UUID, tuple[EvidenceLink, ...]] = Field(default_factory=dict)
