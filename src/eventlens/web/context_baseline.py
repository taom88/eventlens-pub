"""UI-neutral projections for prior context cards and deterministic baselines.

This is a read-side adapter.  It accepts already persisted JSON artifacts and
does not call providers or recompute reactions, confidence intervals, costs, or
valuation.  The models intentionally contain human-readable text as well as
identity/checksum metadata: an identifier is provenance support, not a card.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SHA256 = r"^[a-f0-9]{64}$"
_MISSING = "Not available in the persisted artifact; no value was inferred."


class ContextProjectionError(ValueError):
    """Raised when an artifact cannot safely be projected for human display."""


class DownloadDescriptor(BaseModel):
    """Safe download metadata and, when loaded locally, immutable bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str = Field(min_length=1)
    media_type: Literal["application/json", "text/markdown"]
    content: bytes | None = None
    path: str | None = None
    sha256: str | None = Field(default=None, pattern=SHA256)
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def descriptor_has_content_or_path(self) -> "DownloadDescriptor":
        if self.content is None and self.path is None:
            raise ValueError("download descriptor requires bytes or a safe path")
        if self.content is not None:
            digest = hashlib.sha256(self.content).hexdigest()
            if self.sha256 is not None and self.sha256 != digest:
                raise ValueError("download checksum does not match content")
            if self.size_bytes is not None and self.size_bytes != len(self.content):
                raise ValueError("download size does not match content")
        return self


class SourceSupport(BaseModel):
    """Human-readable provenance, with checksum as audit support."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    provider: str | None = None
    url: str | None = None
    checksum: str | None = Field(default=None, pattern=SHA256)
    citation: str = Field(min_length=1)


class MissingValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PreEventFeaturesView(BaseModel):
    """Typed cutoff-bounded features displayed on a historical card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    article_count: int | None = Field(default=None, ge=0)
    source_count: int | None = Field(default=None, ge=0)
    mean_ticker_relevance: Decimal | None = None
    weighted_sentiment: Decimal | None = None
    recency_weighted_sentiment: Decimal | None = None
    sentiment_positive_share: Decimal | None = None
    latest_close: Decimal | None = None
    prior_5d_return_pct: Decimal | None = None
    prior_20d_return_pct: Decimal | None = None
    missing_values: tuple[MissingValue, ...] = ()


class ReactionMetricsView(BaseModel):
    """Typed realized reaction windows; null means unavailable, never zero."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    after_hours_return_pct: Decimal | None = None
    next_session_open_vs_reference_close_pct: Decimal | None = None
    t_plus_1_close_return_pct: Decimal | None = None
    t_plus_5_close_return_pct: Decimal | None = None
    absolute_next_session_open_move_pct: Decimal | None = None
    missing_values: tuple[MissingValue, ...] = ()

    @property
    def next_open_return_pct(self) -> Decimal | None:
        """Compatibility alias for older card payloads."""

        return self.next_session_open_vs_reference_close_pct


class ContextCardView(BaseModel):
    """A complete, readable prior-event card suitable for a human UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    event_date: date
    cutoff_at: datetime | None = None
    executive_summary: str = Field(min_length=1)
    key_messages: tuple[str, ...] = Field(min_length=1)
    metric_interpretation: str = Field(min_length=1)
    reaction_interpretation: str = Field(min_length=1)
    pre_event_features: PreEventFeaturesView
    reaction_metrics: ReactionMetricsView
    provenance: tuple[SourceSupport, ...] = ()
    sample_size: int | None = Field(default=None, ge=0)
    sample_size_caveat: str = Field(min_length=1)
    regime_caveats: tuple[str, ...] = ()
    missing_values: tuple[MissingValue, ...] = ()
    rights_warnings: tuple[str, ...] = ()
    content_sha256: str = Field(pattern=SHA256)
    json_download: DownloadDescriptor
    markdown_download: DownloadDescriptor

    @model_validator(mode="after")
    def cannot_be_checksum_only(self) -> "ContextCardView":
        if not self.executive_summary.strip() or not any(item.strip() for item in self.key_messages):
            raise ValueError("human-readable card content is required")
        return self


class ContextCardsView(BaseModel):
    """Stable, bounded collection of zero through three prior cards."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cards: tuple[ContextCardView, ...] = Field(max_length=3)
    status: Literal["empty", "available"]
    missing_reason: str | None = None

    @model_validator(mode="after")
    def status_matches_cards(self) -> "ContextCardsView":
        if self.status == "empty" and self.cards:
            raise ValueError("empty context view cannot contain cards")
        if self.status == "available" and not self.cards:
            raise ValueError("available context view requires cards")
        if not self.cards and not self.missing_reason:
            raise ValueError("empty context view requires an explicit reason")
        return self


class BaselineMetricView(BaseModel):
    """One explicitly labelled metric; signed and absolute are never conflated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Literal["signed", "absolute"]
    value_pct: Decimal | None = None
    interpretation: str = Field(min_length=1)
    missing_reason: str | None = None

    @model_validator(mode="after")
    def value_and_reason_are_coherent(self) -> "BaselineMetricView":
        if self.value_pct is None and not self.missing_reason:
            raise ValueError("missing baseline metric requires a reason")
        if self.value_pct is not None and self.missing_reason is not None:
            raise ValueError("present baseline metric cannot have a missing reason")
        if self.label == "absolute" and self.value_pct is not None and self.value_pct < 0:
            raise ValueError("absolute reaction must be non-negative")
        return self


class ConfidenceIntervalView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    estimate_pct: Decimal | None = None
    lower_pct: Decimal | None = None
    upper_pct: Decimal | None = None
    confidence_level: Decimal | None = None
    sample_size: int | None = Field(default=None, ge=0)
    missing_reason: str | None = None

    @model_validator(mode="after")
    def interval_is_complete_or_explained(self) -> "ConfidenceIntervalView":
        values = (self.estimate_pct, self.lower_pct, self.upper_pct)
        if any(value is not None for value in values) and any(value is None for value in values):
            raise ValueError("confidence interval must include estimate, lower, and upper")
        if all(value is None for value in values) and not self.missing_reason:
            raise ValueError("missing confidence interval requires an explicit reason")
        if self.lower_pct is not None and not self.lower_pct <= self.estimate_pct <= self.upper_pct:  # type: ignore[operator]
            raise ValueError("confidence interval bounds must contain estimate")
        return self


class SupplementView(BaseModel):
    """Benchmark/cost context, deliberately separate from the primary metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Literal["benchmark", "transaction_cost"]
    available: bool
    summary: str = Field(min_length=1)
    sample_size: int | None = Field(default=None, ge=0)
    benchmark_id: str | None = None
    missing_reason: str | None = None

    @model_validator(mode="after")
    def available_state_is_explicit(self) -> "SupplementView":
        if not self.available and not self.missing_reason:
            raise ValueError("unavailable supplement requires a reason")
        return self


class MechanicalGuardrailsView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    summary: str = Field(min_length=1)
    thresholds: tuple[str, ...] = ()
    missing_reason: str | None = None

    @model_validator(mode="after")
    def unavailable_guardrails_are_explicit(self) -> "MechanicalGuardrailsView":
        if not self.available and not self.missing_reason:
            raise ValueError("unavailable guardrails require a reason")
        return self


class ValuationView(BaseModel):
    """Actual valuation basis; never inferred from mechanical thresholds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    summary: str = Field(min_length=1)
    methodology: str | None = None
    missing_reason: str | None = None

    @model_validator(mode="after")
    def unavailable_valuation_is_explicit(self) -> "ValuationView":
        if not self.available and not self.missing_reason:
            raise ValueError("unavailable valuation requires a reason")
        return self


class DeterministicBaselineView(BaseModel):
    """Read-only deterministic baseline, without decision or valuation claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    event_id: str | None = None
    signed_reaction: BaselineMetricView
    absolute_reaction: BaselineMetricView
    sample_size: int = Field(ge=0)
    confidence_interval: ConfidenceIntervalView
    benchmark_supplement: SupplementView
    transaction_supplement: SupplementView
    mechanical_guardrails: MechanicalGuardrailsView
    valuation: ValuationView
    provenance: tuple[SourceSupport, ...] = ()
    artifact_sha256: str = Field(pattern=SHA256)
    json_download: DownloadDescriptor
    markdown_download: DownloadDescriptor

    @model_validator(mode="after")
    def signed_and_absolute_labels_are_distinct(self) -> "DeterministicBaselineView":
        if self.signed_reaction.label != "signed" or self.absolute_reaction.label != "absolute":
            raise ValueError("baseline must preserve signed and absolute labels")
        return self


def load_context_cards(source: Any, *, max_cards: int = 3) -> ContextCardsView:
    """Load zero to three cards from a JSON artifact or already parsed payload."""

    if max_cards != 3:
        raise ValueError("MVP-203 context projection supports exactly three prior cards")
    payload, source_path, source_bytes = _read_payload(source)
    if payload is None:
        return ContextCardsView(cards=(), status="empty", missing_reason="No prior context cards were persisted.")
    if isinstance(payload, Mapping) and "cards" in payload:
        raw_cards = payload["cards"]
    elif isinstance(payload, Mapping):
        raw_cards = [payload]
    else:
        raw_cards = payload
    if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, (str, bytes, bytearray)):
        raise ContextProjectionError("context-card artifact must contain a card object or cards array")
    if len(raw_cards) > 3:
        raise ContextProjectionError("context-card artifact contains more than three prior cards")
    views = tuple(
        _project_card(item, source_path=source_path, source_bytes=source_bytes)
        for item in raw_cards
    )
    if len({item.card_id for item in views}) != len(views):
        raise ContextProjectionError("context-card artifact contains duplicate card IDs")
    ordered = tuple(sorted(views, key=lambda item: (item.event_date, item.card_id)))
    if not ordered:
        return ContextCardsView(cards=(), status="empty", missing_reason="Artifact contains no prior context cards.")
    return ContextCardsView(cards=ordered, status="available")


def load_deterministic_baseline(source: Any) -> DeterministicBaselineView:
    """Project an already-computed event-study/replay baseline without recalculation."""

    payload, source_path, source_bytes = _read_payload(source)
    if not isinstance(payload, Mapping):
        raise ContextProjectionError("baseline artifact must be a JSON object")
    return _project_baseline(payload, source_path=source_path, source_bytes=source_bytes)


# Descriptive aliases used by callers that call this a read model.
project_context_cards = load_context_cards
project_deterministic_baseline = load_deterministic_baseline
BaselineView = DeterministicBaselineView


def _read_payload(source: Any) -> tuple[Any, Path | None, bytes | None]:
    if source is None:
        return None, None, None
    source_path: Path | None = None
    source_bytes: bytes | None = None
    if isinstance(source, Path):
        source_path = source.resolve()
        try:
            source_bytes = source_path.read_bytes()
            return json.loads(source_bytes), source_path, source_bytes
        except (OSError, json.JSONDecodeError) as error:
            raise ContextProjectionError(f"cannot read JSON artifact: {source_path}") from error
    if isinstance(source, (bytes, bytearray)):
        source_bytes = bytes(source)
        try:
            return json.loads(source_bytes), None, source_bytes
        except json.JSONDecodeError as error:
            raise ContextProjectionError("artifact bytes are not valid JSON") from error
    if isinstance(source, str):
        try:
            source_bytes = source.encode("utf-8")
            return json.loads(source), None, source_bytes
        except json.JSONDecodeError as error:
            raise ContextProjectionError("artifact text is not valid JSON") from error
    return source, None, None


def _project_card(raw: Any, *, source_path: Path | None, source_bytes: bytes | None) -> ContextCardView:
    if not isinstance(raw, Mapping):
        raise ContextProjectionError("each context card must be an object")
    card = dict(raw)
    try:
        card_id = _required_text(card, "card_id")
        ticker = _required_text(card, "ticker").upper()
        event_id = _required_text(card, "event_id")
        event_date = _required_date(card, "event_date")
    except (KeyError, TypeError, ValueError) as error:
        raise ContextProjectionError(f"malformed context card identity: {error}") from error
    human = _mapping(card.get("human_readable"))
    feature = _mapping(card.get("pre_event_features")) or _mapping(card.get("feature_snapshot"))
    outcome = _mapping(card.get("reaction_metrics")) or _mapping(card.get("historical_outcome"))
    study = _mapping(card.get("event_study"))
    summary = _text(human.get("executive_summary")) or _text(card.get("executive_summary"))
    if not summary:
        raise ContextProjectionError(f"card {card_id} has no executive summary")
    messages = _texts(human.get("key_messages")) or _texts(card.get("key_messages"))
    if not messages:
        raise ContextProjectionError(f"card {card_id} has no key messages")
    interpretation = _text(human.get("interpretation")) or _text(card.get("interpretation"))
    metric_text = (
        _text(card.get("metric_interpretation"))
        or _metric_interpretation(feature)
    )
    reaction_text = _reaction_interpretation(outcome, study)
    if not outcome and not study and card.get("origin") == "fixture_replay":
        reaction_text = f"Persisted fixture-replay reaction summary — {summary}"
    if interpretation:
        reaction_text = interpretation
    caveats = (
        _texts(human.get("caveats"))
        + _texts(card.get("limitations"))
        + _texts(card.get("data_quality_caveats"))
    )
    rights = _rights_warnings(card, human, caveats)
    missing = list(_missing_feature_values(feature))
    missing_fields = {item.field for item in missing}
    missing.extend(item for item in _missing_values(outcome) if item.field not in missing_fields)
    sample_size = _int_or_none(
        _first(study, "comparable_count", "sample_size")
        if study
        else _first(outcome, "sample_size")
    )
    sample_caveat = (
        f"Descriptive historical sample: {sample_size} comparable observation(s); this is not a recommendation."
        if sample_size is not None
        else "Sample size is not present in the card artifact; no confidence or regime inference was made."
    )
    if sample_size is None:
        missing.append(MissingValue(field="sample_size", reason="No comparable-observation count was persisted."))
    provenance = _provenance(card, human)
    raw_json = source_bytes or _canonical_bytes(card)
    digest = _digest(_text(card.get("content_sha256")) or raw_json)
    if len(digest) != 64:
        digest = hashlib.sha256(raw_json).hexdigest()
    json_download = _download("context-card.json", "application/json", raw_json, source_path)
    markdown = _markdown_card(ticker, event_id, summary, messages, metric_text, reaction_text, caveats, rights, provenance)
    markdown_download = _download("context-card.md", "text/markdown", markdown, None)
    cutoff = _datetime_or_none(
        _first(card, "cutoff_at", "knowledge_cutoff_at", "available_at")
    )
    return ContextCardView(
        card_id=card_id, ticker=ticker, event_id=event_id, event_date=event_date,
        cutoff_at=cutoff, executive_summary=summary, key_messages=tuple(messages),
        metric_interpretation=metric_text, reaction_interpretation=reaction_text,
        pre_event_features=_pre_event_features_view(feature),
        reaction_metrics=_reaction_metrics_view(outcome),
        provenance=provenance, sample_size=sample_size, sample_size_caveat=sample_caveat,
        regime_caveats=tuple(caveats), missing_values=tuple(missing), rights_warnings=tuple(rights),
        content_sha256=digest, json_download=json_download, markdown_download=markdown_download,
    )


def _project_baseline(raw: Mapping[str, Any], *, source_path: Path | None, source_bytes: bytes | None) -> DeterministicBaselineView:
    payload = dict(raw)
    nested = _mapping(payload.get("baseline"))
    event_study = _mapping(payload.get("event_study")) or _mapping(nested.get("event_study"))
    if not event_study:
        # Event-study reports are commonly persisted at the artifact root.
        event_study = nested or payload
    quant = _mapping(payload.get("quant_analysis")) or _mapping(nested.get("quant_analysis"))
    reaction = _mapping(payload.get("reaction")) or _mapping(quant.get("reaction")) or _mapping(nested.get("reaction"))
    ticker = _text(payload.get("ticker")) or _text(nested.get("ticker"))
    if not ticker:
        raise ContextProjectionError("baseline artifact has no ticker")
    event_id = _text(payload.get("event_id")) or _text(nested.get("event_id"))
    summary = _mapping(event_study.get("next_open_summary"))
    if not summary:
        # Canonical event-study reports use the metric-specific bootstrap key.
        summary = {"bootstrap": event_study.get("next_open_bootstrap")}
    stats = _mapping(summary.get("statistics"))
    signed_raw = _first(payload, "signed_reaction_pct", "signed_move_pct", "next_open_return_pct")
    signed_raw = signed_raw if signed_raw is not None else _first(reaction, "signed_reaction_pct", "signed_pct", "signed_return_pct", "next_open_return_pct")
    signed_raw = signed_raw if signed_raw is not None else _first(quant, "signed_reaction_pct", "next_open_return_pct")
    signed_raw = signed_raw if signed_raw is not None else _first(nested, "signed_reaction_pct", "next_open_return_pct")
    signed_raw = signed_raw if signed_raw is not None else _first(stats, "mean_pct", "estimate_pct")
    signed_raw = signed_raw if signed_raw is not None else _first(_mapping(summary.get("bootstrap")), "estimate_pct")
    absolute_raw = _first(payload, "absolute_reaction_pct", "absolute_move_pct", "next_open_absolute_move_pct")
    absolute_raw = absolute_raw if absolute_raw is not None else _first(reaction, "absolute_reaction_pct", "absolute_pct", "absolute_move_pct", "next_open_absolute_move_pct")
    absolute_raw = absolute_raw if absolute_raw is not None else _first(quant, "absolute_reaction_pct", "next_open_absolute_move_pct")
    absolute_raw = absolute_raw if absolute_raw is not None else _first(nested, "absolute_reaction_pct", "next_open_absolute_move_pct")
    signed = _metric("signed", signed_raw, "Signed next-session-open reaction versus the pre-event reference close.", "No signed reaction was persisted.")
    absolute = _metric("absolute", absolute_raw, "Absolute next-session-open move; this value must be supplied by the artifact.", "No absolute reaction was persisted; it was not derived from the signed value.")
    sample = _int_or_none(_first(payload, "sample_size", "comparable_count", "event_count", "n"))
    if sample is None:
        sample = _int_or_none(_first(reaction, "sample_size", "comparable_count", "event_count", "n"))
    if sample is None:
        sample = _int_or_none(_first(quant, "sample_size", "comparable_count", "event_count", "n"))
    if sample is None:
        sample = _int_or_none(_first(nested, "sample_size", "comparable_count", "event_count", "n"))
    if sample is None:
        sample = _int_or_none(_first(stats, "sample_count"))
    if sample is None:
        raise ContextProjectionError("baseline artifact has no sample size")
    bootstrap = _mapping(_first(summary, "bootstrap")) or _mapping(payload.get("confidence_interval")) or _mapping(quant.get("confidence_interval")) or _mapping(reaction.get("confidence_interval"))
    ci_reason = _text(_first(payload, "confidence_interval_missing_reason", "ci_missing_reason")) or _text(_first(quant, "confidence_interval_missing_reason", "ci_missing_reason"))
    if not bootstrap:
        minimum = _int_or_none(_first(event_study, "minimum_ci_observations"))
        ci_reason = ci_reason or (f"Confidence interval unavailable: sample size {sample} is below the minimum of {minimum}." if minimum is not None and sample < minimum else "Confidence interval was not persisted in the baseline artifact.")
    ci = ConfidenceIntervalView(
        estimate_pct=_decimal_or_none(_first(bootstrap, "estimate_pct", "mean_pct")),
        lower_pct=_decimal_or_none(_first(bootstrap, "lower_pct")),
        upper_pct=_decimal_or_none(_first(bootstrap, "upper_pct")),
        confidence_level=_decimal_or_none(_first(bootstrap, "confidence_level")),
        sample_size=_int_or_none(_first(bootstrap, "sample_count")) or sample,
        missing_reason=ci_reason if not bootstrap else None,
    )
    supplements = _mapping(payload.get("supplement")) or _mapping(nested.get("supplement"))
    benchmark_raw = _first(payload, "benchmark_supplement", "benchmark") or _first(quant, "benchmark_supplement", "benchmark") or _first(nested, "benchmark_supplement", "benchmark") or supplements.get("benchmark")
    transaction_raw = _first(payload, "transaction_supplement", "transaction_costs", "costs") or _first(quant, "transaction_supplement", "transaction_costs", "costs") or _first(nested, "transaction_supplement", "transaction_costs", "costs") or supplements.get("transaction_costs") or supplements.get("costs")
    benchmark = _supplement("benchmark", benchmark_raw, "No benchmark-relative supplement was persisted.")
    transaction = _supplement("transaction_cost", transaction_raw, "No transaction-cost supplement was persisted.")
    guard_raw = _first(payload, "mechanical_guardrails", "guardrails") or _first(quant, "mechanical_guardrails", "guardrails") or _first(nested, "mechanical_guardrails", "guardrails")
    valuation_raw = _first(payload, "valuation", "valuation_basis") or _first(quant, "valuation", "valuation_basis") or _first(nested, "valuation", "valuation_basis")
    guardrails = _guardrails(guard_raw)
    valuation = _valuation(valuation_raw)
    provenance = _provenance(payload, payload)
    raw_json = source_bytes or _canonical_bytes(payload)
    digest = hashlib.sha256(raw_json).hexdigest()
    markdown = _markdown_baseline(ticker, signed, absolute, sample, ci, benchmark, transaction, guardrails, valuation)
    return DeterministicBaselineView(
        ticker=ticker.upper(), event_id=event_id, signed_reaction=signed, absolute_reaction=absolute,
        sample_size=sample, confidence_interval=ci, benchmark_supplement=benchmark,
        transaction_supplement=transaction, mechanical_guardrails=guardrails, valuation=valuation,
        provenance=provenance, artifact_sha256=digest,
        json_download=_download("deterministic-baseline.json", "application/json", raw_json, source_path),
        markdown_download=_download("deterministic-baseline.md", "text/markdown", markdown, None),
    )


def _required_text(mapping: Mapping[str, Any], key: str) -> str:
    value = _text(mapping.get(key))
    if not value:
        raise ValueError(f"missing {key}")
    return value


def _required_date(mapping: Mapping[str, Any], key: str) -> date:
    value = mapping.get(key)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {key}") from error


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _texts(value: Any) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, (list, tuple)) else []


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ContextProjectionError(f"invalid numeric baseline value: {value!r}")


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ContextProjectionError(f"invalid sample size: {value!r}")
    return result if result >= 0 else None


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _digest(value: str | bytes) -> str:
    if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()


def _download(filename: str, media_type: Literal["application/json", "text/markdown"], content: bytes, path: Path | None) -> DownloadDescriptor:
    return DownloadDescriptor(filename=filename, media_type=media_type, content=content, path=str(path) if path else None, sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content))


def _metric(label: Literal["signed", "absolute"], value: Any, interpretation: str, missing: str) -> BaselineMetricView:
    parsed = _decimal_or_none(value)
    return BaselineMetricView(label=label, value_pct=parsed, interpretation=interpretation, missing_reason=None if parsed is not None else missing)


def _metric_interpretation(feature: Mapping[str, Any]) -> str:
    if not feature:
        return "Pre-event feature metrics were not persisted in this card; no metric value was inferred."
    parts: list[str] = []
    for key, label in (("prior_5d_return_pct", "prior five-session return"), ("prior_20d_return_pct", "prior twenty-session return"), ("recency_weighted_sentiment", "recency-weighted sentiment")):
        value = _display_text(feature.get(key))
        parts.append(f"{label}: {value}" if value is not None else f"{label}: unavailable")
    return "Plain-language pre-event metrics — " + "; ".join(parts) + "."


def _reaction_interpretation(outcome: Mapping[str, Any], study: Mapping[str, Any]) -> str:
    values = outcome or study
    if not values:
        return "Post-event reaction metrics were not persisted in this card; no reaction was inferred."
    next_open = _first(values, "next_session_open_vs_reference_close_pct", "next_open_return_pct")
    t1 = _first(values, "t_plus_1_close_return_pct")
    t5 = _first(values, "t_plus_5_close_return_pct")
    return f"Plain-language realized reaction — next-session open: {_display_text(next_open) or 'unavailable'}; T+1 close: {_display_text(t1) or 'unavailable'}; T+5 close: {_display_text(t5) or 'unavailable'}."


def _missing_feature_values(feature: Mapping[str, Any]) -> tuple[MissingValue, ...]:
    fields = ("recency_weighted_sentiment", "prior_5d_return_pct", "prior_20d_return_pct", "latest_close")
    persisted = _missing_values(feature)
    persisted_fields = {item.field for item in persisted}
    generated = tuple(
        MissingValue(field=field, reason="Field is null or absent in the persisted feature snapshot.")
        for field in fields
        if feature.get(field) is None and field not in persisted_fields
    )
    return persisted + generated


def _display_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, Decimal)):
        text = str(value).strip()
        return text or None
    return None


def _missing_values(value: Mapping[str, Any]) -> tuple[MissingValue, ...]:
    raw = value.get("missing_values")
    if not isinstance(raw, (list, tuple)):
        return ()
    result: list[MissingValue] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContextProjectionError("missing_values entries must be objects")
        field = _text(item.get("field"))
        reason = _text(item.get("reason"))
        if not field or not reason or field in seen:
            raise ContextProjectionError("missing_values entries require unique field and reason")
        seen.add(field)
        result.append(MissingValue(field=field, reason=reason))
    return tuple(result)


def _pre_event_features_view(feature: Mapping[str, Any]) -> PreEventFeaturesView:
    values: dict[str, Any] = {}
    for field in (
        "article_count", "source_count", "mean_ticker_relevance", "weighted_sentiment",
        "recency_weighted_sentiment", "sentiment_positive_share", "latest_close",
        "prior_5d_return_pct", "prior_20d_return_pct",
    ):
        raw = feature.get(field)
        values[field] = _int_or_none(raw) if field in {"article_count", "source_count"} else _decimal_or_none(raw)
    values["missing_values"] = _missing_values(feature)
    return PreEventFeaturesView(**values)


def _reaction_metrics_view(reaction: Mapping[str, Any]) -> ReactionMetricsView:
    values = {
        "after_hours_return_pct": _decimal_or_none(_first(reaction, "after_hours_return_pct", "after_hours_pct")),
        "next_session_open_vs_reference_close_pct": _decimal_or_none(_first(reaction, "next_session_open_vs_reference_close_pct", "next_open_return_pct")),
        "t_plus_1_close_return_pct": _decimal_or_none(_first(reaction, "t_plus_1_close_return_pct", "t_plus_1_return_pct")),
        "t_plus_5_close_return_pct": _decimal_or_none(_first(reaction, "t_plus_5_close_return_pct", "t_plus_5_return_pct")),
        "absolute_next_session_open_move_pct": _decimal_or_none(_first(reaction, "absolute_next_session_open_move_pct", "absolute_next_open_move_pct")),
        "missing_values": _missing_values(reaction),
    }
    return ReactionMetricsView(**values)


def _rights_warnings(card: Mapping[str, Any], human: Mapping[str, Any], caveats: Sequence[str]) -> list[str]:
    rights = _text(card.get("rights_status")) or _text(card.get("rights_policy"))
    result = [rights] if rights else []
    if card.get("origin") == "fixture_replay":
        result.append("Fixture replay only; not fresh or live provider content.")
    result.extend(item for item in caveats if any(word in item.lower() for word in ("rights", "body", "metadata-only", "approval")))
    return list(dict.fromkeys(result))


def _provenance(card: Mapping[str, Any], human: Mapping[str, Any]) -> tuple[SourceSupport, ...]:
    values = human.get("provenance") or card.get("source_provenance") or card.get("provenance") or card.get("provenance_manifest")
    manifest = values if isinstance(values, Mapping) else None
    entries: list[SourceSupport] = []
    if isinstance(values, Mapping):
        values = list(values.values())
    if isinstance(values, (list, tuple)):
        for item in values:
            if not isinstance(item, Mapping):
                continue
            source = _text(_first(item, "source", "provider", "provider_name", "artifact")) or "persisted artifact"
            checksum = _text(_first(item, "checksum", "response_sha256", "raw_response_sha256", "source_sha256"))
            if checksum and (len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum)):
                checksum = None
            entries.append(SourceSupport(source=source, provider=_text(item.get("provider_name")), url=_text(item.get("url")), checksum=checksum, citation=f"Source: {source}" + (f" (checksum {checksum})" if checksum else "")))
    if not entries and manifest is not None:
        # Compact manifests may retain only provider response checksums and
        # versions.  Keep them visible as provenance support rather than
        # presenting a checksum-only card.
        checksums = manifest.get("provider_response_shas") or manifest.get("source_response_shas") or ()
        versions = manifest.get("provider_versions") or ()
        if isinstance(checksums, (list, tuple)):
            for index, checksum in enumerate(checksums):
                checksum_text = _text(checksum)
                if checksum_text and len(checksum_text) == 64 and all(c in "0123456789abcdef" for c in checksum_text):
                    version = versions[index] if isinstance(versions, (list, tuple)) and index < len(versions) else None
                    source = f"persisted provider response {index + 1}" + (f" (version {version})" if version else "")
                    entries.append(SourceSupport(source=source, checksum=checksum_text, citation=f"Source: {source} (checksum {checksum_text})"))
    if not entries:
        links = card.get("provenance_links")
        if isinstance(links, (list, tuple)):
            entries.extend(
                SourceSupport(
                    source=str(link),
                    url=str(link),
                    citation=f"Fixture provenance: {link}",
                )
                for link in links
                if str(link).strip()
            )
    return tuple(entries)


def _supplement(label: Literal["benchmark", "transaction_cost"], raw: Any, missing: str) -> SupplementView:
    if not isinstance(raw, Mapping):
        return SupplementView(label=label, available=False, summary=missing, missing_reason=missing)
    sample = _int_or_none(_first(raw, "sample_size", "paired_t_plus_1_count", "sample_count"))
    benchmark_id = _text(_first(raw, "benchmark_id", "ticker"))
    summary = _text(raw.get("summary")) or (f"{label.replace('_', ' ')} supplement is present in the persisted artifact." )
    return SupplementView(label=label, available=True, summary=summary, sample_size=sample, benchmark_id=benchmark_id)


def _guardrails(raw: Any) -> MechanicalGuardrailsView:
    if isinstance(raw, (list, tuple)):
        thresholds = tuple(
            f"{item.get('label', item.get('name', 'guardrail'))}: {item.get('threshold')}"
            for item in raw
            if isinstance(item, Mapping)
        )
        if thresholds:
            return MechanicalGuardrailsView(
                available=True,
                summary="Persisted mechanical guardrails are available; they are not valuation.",
                thresholds=thresholds,
            )
    if not isinstance(raw, Mapping):
        return MechanicalGuardrailsView(available=False, summary="Mechanical guardrails were not persisted.", missing_reason="No mechanical guardrail artifact was supplied.")
    thresholds = tuple(str(item) for item in raw.get("thresholds", ()) if str(item).strip()) if isinstance(raw.get("thresholds"), (list, tuple)) else ()
    return MechanicalGuardrailsView(available=True, summary=_text(raw.get("summary")) or "Persisted mechanical guardrails are available; they are not valuation.", thresholds=thresholds)


def _valuation(raw: Any) -> ValuationView:
    if not isinstance(raw, Mapping):
        return ValuationView(available=False, summary="Actual valuation is not part of this deterministic baseline.", missing_reason="No valuation basis was persisted; mechanical thresholds are not valuation.")
    return ValuationView(available=True, summary=_text(raw.get("summary")) or "A persisted valuation basis is available.", methodology=_text(raw.get("methodology")))


def _markdown_card(ticker: str, event_id: str, summary: str, messages: Sequence[str], metric: str, reaction: str, caveats: Sequence[str], rights: Sequence[str], provenance: Sequence[SourceSupport]) -> bytes:
    caveat_lines = [f"- {item}" for item in caveats] or [f"- {_MISSING}"]
    rights_lines = [f"- {item}" for item in rights] or [f"- {_MISSING}"]
    provenance_lines = [f"- {item.citation}" for item in provenance] or [f"- {_MISSING}"]
    lines = [f"# {ticker} prior context", "", f"Event: {event_id}", "", "## Executive summary", "", summary, "", "## Key messages", *[f"- {item}" for item in messages], "", "## Metric interpretation", metric, "", "## Reaction interpretation", reaction, "", "## Caveats", *caveat_lines, "", "## Rights warnings", *rights_lines, "", "## Provenance", *provenance_lines]
    return ("\n".join(lines) + "\n").encode()


def _markdown_baseline(ticker: str, signed: BaselineMetricView, absolute: BaselineMetricView, sample: int, ci: ConfidenceIntervalView, benchmark: SupplementView, transaction: SupplementView, guardrails: MechanicalGuardrailsView, valuation: ValuationView) -> bytes:
    def value(metric: BaselineMetricView) -> str:
        return str(metric.value_pct) if metric.value_pct is not None else f"unavailable — {metric.missing_reason}"
    lines = [f"# {ticker} deterministic baseline", "", "This is a descriptive mechanical baseline, not an investment recommendation.", "", "## Reaction", f"- Signed reaction (%): {value(signed)}", f"- Absolute reaction (%): {value(absolute)}", f"- Sample size: {sample}", "", "## Confidence interval", f"- Estimate/lower/upper: {ci.estimate_pct}, {ci.lower_pct}, {ci.upper_pct}" if ci.estimate_pct is not None else f"- Unavailable: {ci.missing_reason}", "", "## Supplements", f"- Benchmark: {benchmark.summary}", f"- Transaction costs: {transaction.summary}", "", "## Guardrails versus valuation", f"- Mechanical guardrails: {guardrails.summary}", f"- Actual valuation: {valuation.summary}", ""]
    return "\n".join(lines).encode()


__all__ = [
    "BaselineMetricView", "BaselineView", "ConfidenceIntervalView", "ContextCardView",
    "ContextCardsView", "ContextProjectionError", "DeterministicBaselineView",
    "DownloadDescriptor", "MechanicalGuardrailsView", "MissingValue", "SourceSupport",
    "PreEventFeaturesView", "ReactionMetricsView", "SupplementView", "ValuationView",
    "load_context_cards", "load_deterministic_baseline",
    "project_context_cards", "project_deterministic_baseline",
]
