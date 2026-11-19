"""Deterministic publication gates for EventLens context cards.

QC is deliberately a reporting layer: it does not repair facts, select a
source, or grant approval.  A caller supplies the uses of receipts when a
card is being evaluated for a backtest or decision; absent use declarations
are not guessed from prose.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from .accounting import SurpriseComparison
from .conflicts import ConflictMateriality, ResolutionStatus
from .evidence import AnalyticalUse, EvidenceClass, EvidenceReceipt, permission_for
from .models import ContextCardV4
from .temporal import AvailabilityValue, TemporalClassification, TemporalConfidence


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Confidence = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]


class PublicationStatus(StrEnum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    STALE = "stale"


class QCSeverity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


class QCCode(StrEnum):
    SCHEMA_INVALID = "schema_invalid"
    REQUIRED_FIELD_MISSING = "required_field_missing"
    UNIT_INVALID = "unit_invalid"
    PERIOD_INVALID = "period_invalid"
    CUTOFF_LEAKAGE = "cutoff_leakage"
    RECONSTRUCTED_BACKTEST_INPUT = "reconstructed_backtest_input"
    CONSENSUS_BASIS_UNVERIFIED = "consensus_basis_unverified"
    SURPRISE_COMPARABILITY_UNVALIDATED = "surprise_comparability_unvalidated"
    UNRESOLVED_MATERIAL_CONFLICT = "unresolved_material_conflict"
    DANGLING_EVIDENCE_RECEIPT = "dangling_evidence_receipt"
    DERIVED_OPERANDS_UNVALIDATED = "derived_operands_unvalidated"
    EVIDENCE_PERMISSION_DENIED = "evidence_permission_denied"
    TRANSCRIPT_SPEAKER_UNRESOLVED = "transcript_speaker_unresolved"
    ATTRIBUTION_REPRESENTED_AS_FACT = "attribution_represented_as_fact"
    DECISION_HORIZON_MISSING = "decision_horizon_missing"
    DECISION_THRESHOLD_METHODOLOGY_MISSING = "decision_threshold_methodology_missing"
    DECISION_TRACE_MISSING = "decision_trace_missing"
    TIER_1_MISSING_REASON = "tier_1_missing_reason"
    ONE_CONSENSUS_SOURCE = "one_consensus_source"
    MEDIUM_REACTION_ATTRIBUTION = "medium_reaction_attribution"
    TIER_4_KPI_ABSENT = "tier_4_kpi_absent"
    MACRO_OVERLAP_T_PLUS_5 = "macro_overlap_t_plus_5"
    SECONDARY_TRANSCRIPT_PROVIDER = "secondary_transcript_provider"
    NEW_QUARTER_AVAILABLE = "new_quarter_available"
    EVENT_DATE_CHANGED = "event_date_changed"
    CONSENSUS_AGED = "consensus_aged"


class _QCModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class QCIssue(_QCModel):
    code: QCCode
    severity: QCSeverity
    message: NonEmptyStr
    field_path: NonEmptyStr | None = None

    @property
    def blocking(self) -> bool:
        return self.severity is QCSeverity.BLOCKER


class ConfidenceComponents(_QCModel):
    """Explainable inputs to the documented weighted-confidence calculation."""

    factual_integrity: Confidence
    temporal_integrity: Confidence
    accounting_comparability: Confidence
    consensus_quality: Confidence
    attribution_quality: Confidence
    decision_calibration: Confidence


class QCWarningSignals(_QCModel):
    """Explicit, externally observed conditions that are warnings, not repairs.

    These conditions are not inferable from the normalized card without
    guessing at provider coverage or market context.  Callers must therefore
    declare them; the QC engine records their deterministic publication impact.
    """

    consensus_source_count: int | None = Field(default=None, ge=0)
    absent_tier_4_kpis: tuple[NonEmptyStr, ...] = ()
    macro_overlap_t_plus_5: bool = False
    secondary_transcript_provider: bool = False


class QCReport(_QCModel):
    publication_status: PublicationStatus
    issues: tuple[QCIssue, ...] = ()
    confidence: ConfidenceComponents | None = None
    overall_confidence: Confidence | None = None

    @property
    def blockers(self) -> tuple[QCIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def warnings(self) -> tuple[QCIssue, ...]:
        return tuple(issue for issue in self.issues if not issue.blocking)

    @model_validator(mode="after")
    def report_state_matches_issues(self) -> "QCReport":
        if self.blockers and self.publication_status is not PublicationStatus.BLOCKED:
            raise ValueError("a QC report with blockers must be blocked")
        if self.overall_confidence is None and self.confidence is not None:
            raise ValueError("confidence components require an overall confidence")
        if self.overall_confidence is not None and self.confidence is None:
            raise ValueError("overall confidence requires confidence components")
        return self


_WEIGHTS = {
    "factual_integrity": Decimal("0.25"),
    "temporal_integrity": Decimal("0.25"),
    "accounting_comparability": Decimal("0.15"),
    "consensus_quality": Decimal("0.15"),
    "attribution_quality": Decimal("0.10"),
    "decision_calibration": Decimal("0.10"),
}
_CRITICAL = frozenset({"factual_integrity", "temporal_integrity", "accounting_comparability"})
_LOW_THRESHOLD = Decimal("0.50")
_CRITICAL_CAP = Decimal("0.59")


def aggregate_confidence(components: ConfidenceComponents | Mapping[str, object]) -> Decimal:
    """Return a weighted confidence score with a hard critical-low cap.

    The result is rounded to four decimal places.  If factual, temporal, or
    accounting integrity is below 0.50, the score may not present as high
    confidence regardless of other inputs.
    """

    parsed = components if isinstance(components, ConfidenceComponents) else ConfidenceComponents.model_validate(components)
    weighted = sum(
        getattr(parsed, name) * weight for name, weight in _WEIGHTS.items()
    )
    if any(getattr(parsed, name) < _LOW_THRESHOLD for name in _CRITICAL):
        weighted = min(weighted, _CRITICAL_CAP)
    return weighted.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def validate_card(
    card: ContextCardV4 | Mapping[str, Any],
    *,
    analytical_uses: Mapping[str, Iterable[AnalyticalUse | str]] | None = None,
    tier_1_missing_reasons: Mapping[str, str | None] | None = None,
    confidence: ConfidenceComponents | Mapping[str, object] | None = None,
    freshness_issues: Iterable[QCIssue] = (),
    warning_signals: QCWarningSignals | Mapping[str, object] | None = None,
) -> QCReport:
    """Run publication gates and return structured blockers/warnings.

    ``analytical_uses`` maps receipt IDs to the actual proposed use.  This is
    intentionally explicit: reconstructed evidence can be descriptive but is
    never safe to smuggle into a backtest feature or autonomous decision.
    """

    try:
        parsed_card = (
            ContextCardV4.model_validate(card.model_dump(mode="json"))
            if isinstance(card, ContextCardV4)
            else ContextCardV4.model_validate(card)
        )
    except ValidationError as exc:
        return QCReport(
            publication_status=PublicationStatus.BLOCKED,
            issues=tuple(_schema_issues(exc)),
        )
    card = parsed_card
    issues: list[QCIssue] = []
    receipts = card.evidence_registry.receipts_by_id
    claim_ids = {claim.id for claim in card.evidence_registry.semantic_claims}

    for receipt in card.evidence_registry.receipts:
        _validate_receipt_graph(receipt, receipts, issues)
        if receipt.temporal_context_ref is not None:
            prefix = "temporal:"
            observation_id = receipt.temporal_context_ref.removeprefix(prefix)
            if not receipt.temporal_context_ref.startswith(prefix) or observation_id not in card.temporal_state.observations:
                _block(issues, QCCode.DANGLING_EVIDENCE_RECEIPT, "receipt temporal_context_ref does not resolve to a temporal observation", f"evidence_registry.receipts.{receipt.id}.temporal_context_ref")
    for claim in card.evidence_registry.semantic_claims:
        if claim.evidence.id not in receipts:
            _block(issues, QCCode.DANGLING_EVIDENCE_RECEIPT, "semantic claim has no registered evidence receipt", f"evidence_registry.semantic_claims.{claim.id}")

    for conflict in card.conflicts:
        if conflict.materiality is ConflictMateriality.MATERIAL and conflict.resolution_status is ResolutionStatus.UNRESOLVED:
            _block(issues, QCCode.UNRESOLVED_MATERIAL_CONFLICT, "material metric conflict remains unresolved", f"conflicts.{conflict.id}")

    _validate_eps_surprises(card, issues)
    _validate_temporal_and_permissions(card, receipts, analytical_uses or {}, issues)
    _validate_transcript_and_attribution(card, claim_ids, issues)
    observed_tier_1 = {f"{item.view}:{item.fiscal_period}:{item.ontology_key}": item.missing_reason for item in card.tier_1_observations if item.value is None}
    _validate_tier_1_reasons(observed_tier_1 | dict(tier_1_missing_reasons or {}), issues)
    _validate_decision_trace(card, issues)
    _validate_warning_signals(warning_signals, issues)
    issues.extend(freshness_issues)

    parsed_confidence = (
        None if confidence is None else confidence if isinstance(confidence, ConfidenceComponents) else ConfidenceComponents.model_validate(confidence)
    )
    overall = None if parsed_confidence is None else aggregate_confidence(parsed_confidence)
    status = PublicationStatus.BLOCKED if any(issue.blocking for issue in issues) else _status_from_freshness(issues)
    return QCReport(
        publication_status=status,
        issues=tuple(_unique_issues(issues)),
        confidence=parsed_confidence,
        overall_confidence=overall,
    )


def _schema_issues(exc: ValidationError) -> list[QCIssue]:
    """Convert untrusted v4 JSON validation errors into stable QC blockers."""

    issues: list[QCIssue] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(item) for item in error["loc"])
        kind = str(error["type"])
        if kind == "missing":
            code = QCCode.REQUIRED_FIELD_MISSING
        elif "unit" in error["loc"]:
            code = QCCode.UNIT_INVALID
        elif any("period" in str(item) for item in error["loc"]):
            code = QCCode.PERIOD_INVALID
        else:
            code = QCCode.SCHEMA_INVALID
        _block(issues, code, str(error["msg"]), location or None)
    return _unique_issues(issues)


def detect_staleness(
    card: ContextCardV4,
    *,
    now: datetime | None = None,
    latest_completed_fiscal_period: str | None = None,
    current_event_datetime: datetime | None = None,
    latest_consensus_timestamp: datetime | None = None,
    consensus_max_age: timedelta = timedelta(days=14),
) -> tuple[QCIssue, ...]:
    """Return explicit staleness findings; regeneration time is never evidence.

    The caller supplies authoritative current event/consensus state rather than
    relying on a wall-clock heuristic hidden inside the card.
    """

    if not isinstance(card, ContextCardV4):
        raise TypeError("detect_staleness requires a ContextCardV4")
    if consensus_max_age <= timedelta(0):
        raise ValueError("consensus_max_age must be positive")
    moment = _aware(now or datetime.now(timezone.utc), "now")
    issues: list[QCIssue] = []
    if latest_completed_fiscal_period and latest_completed_fiscal_period != card.current_pre_event_state.latest_reported_fiscal_period:
        _warn(issues, QCCode.NEW_QUARTER_AVAILABLE, "a newer completed fiscal period is available", "current_pre_event_state.latest_reported_fiscal_period")
    if current_event_datetime is not None:
        current = _aware(current_event_datetime, "current_event_datetime")
        recorded = card.identity.event.event_datetime
        if recorded != current:
            _warn(issues, QCCode.EVENT_DATE_CHANGED, "the earnings event date or time has changed", "identity.event.event_datetime")
    if latest_consensus_timestamp is not None:
        observed = _aware(latest_consensus_timestamp, "latest_consensus_timestamp")
        if moment - observed > consensus_max_age:
            _warn(issues, QCCode.CONSENSUS_AGED, "consensus exceeds the configured freshness tolerance", "consensus_state")
    return tuple(issues)


def _validate_receipt_graph(receipt: EvidenceReceipt, receipts: Mapping[str, EvidenceReceipt], issues: list[QCIssue]) -> None:
    missing = [receipt_id for receipt_id in receipt.input_receipt_ids if receipt_id not in receipts]
    if missing:
        _block(issues, QCCode.DANGLING_EVIDENCE_RECEIPT, "evidence receipt references missing inputs: " + ", ".join(sorted(missing)), f"evidence_registry.receipts.{receipt.id}")
    if receipt.evidence_class is EvidenceClass.DERIVED and (not receipt.input_receipt_ids or not receipt.operands_validated):
        _block(issues, QCCode.DERIVED_OPERANDS_UNVALIDATED, "derived evidence lacks validated operand receipts", f"evidence_registry.receipts.{receipt.id}")


def _validate_eps_surprises(card: ContextCardV4, issues: list[QCIssue]) -> None:
    comparisons: list[SurpriseComparison] = []
    for quarter in card.historical_quarters:
        if quarter.surprise_layers.eps_vs_sell_side is not None:
            comparisons.append(quarter.surprise_layers.eps_vs_sell_side)
    for comparison in comparisons:
        if comparison.normalized.status == "blocked":
            _block(issues, QCCode.SURPRISE_COMPARABILITY_UNVALIDATED, "EPS surprise is blocked because its basis or comparison is unvalidated", "historical_quarters.surprise_layers.eps_vs_sell_side")
        if comparison.estimate.accounting_basis.value == "unknown":
            _block(issues, QCCode.CONSENSUS_BASIS_UNVERIFIED, "EPS consensus accounting basis is unverified", "historical_quarters.surprise_layers.eps_vs_sell_side.estimate")


def _validate_temporal_and_permissions(card: ContextCardV4, receipts: Mapping[str, EvidenceReceipt], uses: Mapping[str, Iterable[AnalyticalUse | str]], issues: list[QCIssue]) -> None:
    for receipt_id, raw_uses in uses.items():
        receipt = receipts.get(receipt_id)
        if receipt is None:
            _block(issues, QCCode.DANGLING_EVIDENCE_RECEIPT, "declared analytical use references an unknown receipt", f"analytical_uses.{receipt_id}")
            continue
        for raw_use in raw_uses:
            try:
                use = AnalyticalUse(raw_use)
            except ValueError:
                _block(issues, QCCode.EVIDENCE_PERMISSION_DENIED, "unknown analytical use fails closed", f"analytical_uses.{receipt_id}")
                continue
            if permission_for(receipt, use, registry=card.evidence_registry).value != "allowed":
                code = QCCode.RECONSTRUCTED_BACKTEST_INPUT if receipt.evidence_class is EvidenceClass.RECONSTRUCTED and use in {AnalyticalUse.BACKTEST_FEATURE, AnalyticalUse.BACKTEST_LABEL} else QCCode.EVIDENCE_PERMISSION_DENIED
                _block(issues, code, f"{receipt.evidence_class.value} evidence is not permitted for {use.value}", f"analytical_uses.{receipt_id}")
    for observation_id, temporal in card.temporal_state.observations.items():
        # A caller declaring a receipt as a backtest feature is the binding use;
        # this check catches an explicit temporal observation assigned the same id.
        declared: set[AnalyticalUse] = set()
        for value in uses.get(observation_id, ()):
            try:
                declared.add(AnalyticalUse(value))
            except ValueError:
                # The receipt-level pass above has already emitted a
                # deterministic fail-closed finding for this malformed use.
                continue
        if declared & {AnalyticalUse.BACKTEST_FEATURE, AnalyticalUse.BACKTEST_LABEL} and (
            temporal.available_before_event.value is not AvailabilityValue.AVAILABLE
            or temporal.classification is not TemporalClassification.NATIVE
            or temporal.temporal_confidence is not TemporalConfidence.HIGH
        ):
            _block(issues, QCCode.CUTOFF_LEAKAGE, "backtest input is not proven native and cutoff-safe", f"temporal_state.observations.{observation_id}")


def _validate_transcript_and_attribution(card: ContextCardV4, claim_ids: set[str], issues: list[QCIssue]) -> None:
    for claim in card.evidence_registry.semantic_claims:
        if "unresolved_management_speaker_numeric_guidance" in claim.topics:
            _block(issues, QCCode.TRANSCRIPT_SPEAKER_UNRESOLVED, "numeric management guidance has an unresolved speaker", f"evidence_registry.semantic_claims.{claim.id}")
    reactions = list(card.market_reaction_history) + [quarter.reaction for quarter in card.historical_quarters if quarter.reaction is not None]
    for reaction in reactions:
        for attribution in reaction.attributions:
            if attribution.confidence.value == "medium":
                _warn(issues, QCCode.MEDIUM_REACTION_ATTRIBUTION, "reaction attribution remains medium confidence", "market_reaction_history")
            for candidate in attribution.candidate_drivers:
                if candidate.evidence_class is EvidenceClass.SOURCE_NATIVE:
                    _block(issues, QCCode.ATTRIBUTION_REPRESENTED_AS_FACT, "reaction attribution is represented as source-native fact", f"attribution.{candidate.id}")
                if not set(candidate.semantic_claim_ids).issubset(claim_ids):
                    _block(issues, QCCode.DANGLING_EVIDENCE_RECEIPT, "reaction attribution references an unknown semantic claim", f"attribution.{candidate.id}")


def _validate_tier_1_reasons(missing: Mapping[str, str | None], issues: list[QCIssue]) -> None:
    for kpi_id, reason in missing.items():
        if reason is None or not reason.strip():
            _block(issues, QCCode.TIER_1_MISSING_REASON, f"missing Tier-1 KPI {kpi_id!r} has no explicit reason", f"tier_1_missing_reasons.{kpi_id}")


def _validate_warning_signals(
    signals: QCWarningSignals | Mapping[str, object] | None,
    issues: list[QCIssue],
) -> None:
    if signals is None:
        return
    parsed = signals if isinstance(signals, QCWarningSignals) else QCWarningSignals.model_validate(signals)
    if parsed.consensus_source_count == 1:
        _warn(issues, QCCode.ONE_CONSENSUS_SOURCE, "only one consensus source is available", "consensus_state")
    for kpi_id in parsed.absent_tier_4_kpis:
        _warn(issues, QCCode.TIER_4_KPI_ABSENT, f"Tier-4 KPI {kpi_id!r} is absent", f"tier_4_kpis.{kpi_id}")
    if parsed.macro_overlap_t_plus_5:
        _warn(issues, QCCode.MACRO_OVERLAP_T_PLUS_5, "macro event overlap limits T+5 reaction interpretation", "market_reaction_history")
    if parsed.secondary_transcript_provider:
        _warn(issues, QCCode.SECONDARY_TRANSCRIPT_PROVIDER, "transcript came from a secondary provider", "evidence_registry")


def _validate_decision_trace(card: ContextCardV4, issues: list[QCIssue]) -> None:
    decision = card.decision.model_dump(mode="json")
    decision_bearing = any(decision.get(key) not in (None, "not_evaluated") for key in ("action", "recommendation", "status")) and decision.get("status") != "not_evaluated"
    if not decision_bearing:
        return
    horizon = decision.get("horizon")
    legacy_horizon_present = (
        getattr(card.decision_context, "horizon_start", None) is not None
        and getattr(card.decision_context, "horizon_end", None) is not None
    )
    if not horizon and not legacy_horizon_present:
        _block(issues, QCCode.DECISION_HORIZON_MISSING, "decision-bearing card requires a declared horizon", "decision_context")
    threshold = decision.get("entry_threshold")
    if not isinstance(threshold, Mapping):
        legacy_threshold = card.decision_context.entry_threshold
        threshold = (
            legacy_threshold.model_dump(mode="json")
            if hasattr(legacy_threshold, "model_dump")
            else {"status": getattr(legacy_threshold, "status", None)}
        )
    if threshold.get("status") == "computed" and not all(
        threshold.get(field) not in (None, (), [], "")
        for field in ("methodology", "inputs", "computed_at", "sensitivity")
    ) and not decision.get("threshold_methodology"):
        _block(issues, QCCode.DECISION_THRESHOLD_METHODOLOGY_MISSING, "numeric decision threshold requires methodology, inputs, timestamp, and sensitivity", "decision.entry_threshold")
    trace = decision.get("trace")
    if not trace:
        _block(issues, QCCode.DECISION_TRACE_MISSING, "decision-bearing card requires an applicable decision trace", "decision")
        return
    registry = getattr(card, "evidence_registry", None)
    receipts = getattr(registry, "receipts_by_id", None)
    if not isinstance(receipts, Mapping):
        return
    trace_fields = (
        "input_evidence_receipt_ids",
        "threshold_input_receipt_ids",
        "scenario_calibration_evidence_receipt_ids",
        "trigger_evidence_receipt_ids",
        "invalidation_evidence_receipt_ids",
        "long_term_evidence_receipt_ids",
    )
    for receipt_id in dict.fromkeys(
        str(receipt_id)
        for field in trace_fields
        for receipt_id in trace.get(field, ())
    ):
        receipt = receipts.get(receipt_id)
        if receipt is None:
            _block(issues, QCCode.DANGLING_EVIDENCE_RECEIPT, "decision trace references an unknown evidence receipt", f"decision.trace.{receipt_id}")
        elif permission_for(
            receipt,
            AnalyticalUse.AUTONOMOUS_DECISION,
            registry=registry,
        ).value != "allowed":
            _block(issues, QCCode.EVIDENCE_PERMISSION_DENIED, "decision trace contains evidence prohibited from decision use", f"decision.trace.{receipt_id}")


def _status_from_freshness(issues: list[QCIssue]) -> PublicationStatus:
    stale = {QCCode.NEW_QUARTER_AVAILABLE, QCCode.EVENT_DATE_CHANGED, QCCode.CONSENSUS_AGED}
    return PublicationStatus.STALE if any(issue.code in stale for issue in issues) else PublicationStatus.REVIEWED


def _block(issues: list[QCIssue], code: QCCode, message: str, path: str | None = None) -> None:
    issues.append(QCIssue(code=code, severity=QCSeverity.BLOCKER, message=message, field_path=path))


def _warn(issues: list[QCIssue], code: QCCode, message: str, path: str | None = None) -> None:
    issues.append(QCIssue(code=code, severity=QCSeverity.WARNING, message=message, field_path=path))


def _unique_issues(issues: Iterable[QCIssue]) -> list[QCIssue]:
    seen: set[tuple[str, str | None]] = set()
    result: list[QCIssue] = []
    for issue in issues:
        key = (issue.code.value, issue.field_path)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)
