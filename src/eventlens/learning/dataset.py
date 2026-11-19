"""Versioned evaluation dataset construction and leakage-safe temporal splits."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from eventlens.graph.models import WorkflowState

from .models import (
    ApprovedPostmortem,
    EvaluationExample,
    OutcomeRecord,
    PreEventFeature,
    evaluation_content_checksum,
)


def migrate_evaluation_document(document: dict) -> dict:
    """Explicit no-op migration for the initial stable schema."""

    version = document.get("schema_version")
    if version == "1.0":
        return dict(document)
    raise ValueError(f"unsupported evaluation schema version: {version!r}")


def build_evaluation_example(
    state: WorkflowState,
    outcome: OutcomeRecord,
    postmortem: ApprovedPostmortem,
    *,
    provider_availability: dict[str, bool],
    sector: str | None = None,
    industry: str | None = None,
    market_regime: str | None = None,
) -> EvaluationExample:
    if state.decision_packet is None or state.locked_hypothesis is None:
        raise ValueError("evaluation requires a completed pre-event decision ledger")
    if state.stage.value != "completed":
        raise ValueError("evaluation requires an approved completed workflow")
    packet = state.decision_packet
    features = tuple(
        PreEventFeature(
            feature_id=metric.metric_id,
            value=metric.rendered_value,
            available_at=state.information_cutoff_at,
            source_id=packet.snapshot.snapshot_id,
        )
        for metric in packet.snapshot.canonical_metrics
    )
    expected_move = (
        Decimal(str(state.expected_move.expected_move_pct))
        if state.expected_move is not None
        else None
    )
    payload = {
        "schema_version": "1.0",
        "example_id": f"eval:{state.session_id}:{state.event_id}",
        "created_at": outcome.collected_at,
        "information_cutoff_at": state.information_cutoff_at,
        "session_id": state.session_id,
        "event_id": state.event_id,
        "ticker": state.ticker,
        "event_date": state.event_date,
        "sector": sector,
        "industry": industry,
        "market_regime": market_regime,
        "event_type": "earnings",
        "pre_event_features": features,
        "locked_hypothesis": state.locked_hypothesis,
        "decision_packet": packet,
        "expected_move_estimate_pct": expected_move,
        "expected_move_type": packet.canonical_expected_move_status,
        "outcome": outcome,
        "approved_postmortem": postmortem,
        "provider_availability": provider_availability,
        "model_versions": tuple(sorted({item.model for item in packet.desk_reports})),
        "prompt_versions": tuple(
            sorted({item.prompt_version for item in packet.desk_reports})
        ),
        "quant_parameters": {
            item.metric_id: item.calculation_version
            for item in packet.snapshot.canonical_metrics
        },
        "human_approvals": (
            "event_confirmation",
            "hypothesis_lock",
            "decision_packet_approval",
            "postmortem_approval",
        ),
    }
    checksum = evaluation_content_checksum(
        EvaluationExample.model_construct(
            **payload, content_sha256="0" * 64
        ).model_dump(mode="json", exclude={"content_sha256"})
    )
    return EvaluationExample.model_validate(
        {**payload, "content_sha256": checksum}
    )


def temporal_ticker_disjoint_split(
    examples: Iterable[EvaluationExample],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
) -> dict[str, tuple[EvaluationExample, ...]]:
    """Split by ticker group ordered by earliest event, preventing ticker leakage."""

    if train_fraction <= 0 or validation_fraction <= 0:
        raise ValueError("train and validation fractions must be positive")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("test fraction must remain positive")
    groups: dict[str, list[EvaluationExample]] = defaultdict(list)
    for example in examples:
        groups[example.ticker].append(example)
    ordered_groups = sorted(
        (tuple(sorted(items, key=lambda item: item.event_date)) for items in groups.values()),
        key=lambda items: items[0].event_date,
    )
    count = len(ordered_groups)
    if count < 3:
        raise ValueError("at least three ticker groups are required for disjoint splits")
    train_end = max(1, int(count * train_fraction))
    validation_end = max(train_end + 1, int(count * (train_fraction + validation_fraction)))
    validation_end = min(validation_end, count - 1)
    partitions = {
        "train": ordered_groups[:train_end],
        "validation": ordered_groups[train_end:validation_end],
        "test": ordered_groups[validation_end:],
    }
    return {
        name: tuple(item for group in selected for item in group)
        for name, selected in partitions.items()
    }
