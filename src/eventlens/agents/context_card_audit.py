"""Provider-free adversarial interpreter for Context Card v4 release audits."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from eventlens.context_cards.audit import (
    AuditCategory,
    AuditFinding,
    AuditSeverity,
)
from eventlens.context_cards.qc import QCReport


@runtime_checkable
class ContextCardAuditor(Protocol):
    """Boundary for an independent auditor; implementations must be read-only."""

    auditor_id: str

    def audit(
        self, card_document: Mapping[str, Any], *, qc_report: QCReport
    ) -> tuple[AuditFinding, ...]: ...


class DeterministicContextCardAuditor:
    """A deterministic red-team pass used offline and as a model-audit backstop."""

    auditor_id = "eventlens.context-card-adversarial.deterministic.v1"

    def audit(
        self, card_document: Mapping[str, Any], *, qc_report: QCReport
    ) -> tuple[AuditFinding, ...]:
        del qc_report  # Independent detectors do not reinterpret QC conclusions.
        findings: list[AuditFinding] = []
        _audit_temporal(card_document, findings)
        _audit_accounting(card_document, findings)
        _audit_evidence(card_document, findings)
        _audit_speakers(card_document, findings)
        _audit_attribution(card_document, findings)
        _audit_staleness(card_document, findings)
        _audit_precision_and_nulls(card_document, findings)
        _audit_horizons(card_document, findings)
        _audit_embedded_release_state(card_document, findings)
        return tuple(findings)


def _finding(
    findings: list[AuditFinding],
    *,
    category: AuditCategory,
    path: str,
    failure: str,
    inference: str,
    prevention: str,
    severity: AuditSeverity = AuditSeverity.MATERIAL,
) -> None:
    findings.append(
        AuditFinding(
            finding_id=f"{category.value}-{len(findings) + 1}",
            category=category,
            field_path=path,
            failure_mode=failure,
            downstream_inference=inference,
            severity=severity,
            deterministic_prevention=prevention,
        )
    )


def _audit_temporal(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    temporal = _mapping(card.get("temporal_state"))
    cutoff = temporal.get("historical_event_cutoff")
    for observation_id, raw in _mapping(temporal.get("observations")).items():
        observation = _mapping(raw)
        availability = _mapping(observation.get("available_before_event")).get("value")
        first_known = observation.get("first_known_available_at")
        path = f"temporal_state.observations.{observation_id}"
        if availability != "available" or (
            cutoff is not None and first_known is not None and str(first_known) >= str(cutoff)
        ):
            _finding(
                findings,
                category=AuditCategory.TEMPORAL_LEAKAGE,
                path=path,
                failure="Observation is not proven available strictly before the historical cutoff.",
                inference="A downstream agent could convert later-known information into apparent pre-event foresight.",
                prevention="Permit historical decision and backtest use only when available_before_event=available and first_known_available_at is before the cutoff.",
                severity=AuditSeverity.CRITICAL,
            )


def _audit_accounting(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    for q_index, raw_quarter in enumerate(_sequence(card.get("historical_quarters"))):
        quarter = _mapping(raw_quarter)
        quarter_period = quarter.get("fiscal_period")
        base = f"historical_quarters.{q_index}"
        for e_index, raw_expectation in enumerate(_sequence(quarter.get("expectations"))):
            expectation = _mapping(raw_expectation)
            if expectation.get("fiscal_period") != quarter_period:
                _period_basis_finding(findings, f"{base}.expectations.{e_index}.fiscal_period")
            for metric_name, raw_metric in _mapping(expectation.get("metrics")).items():
                metric = _mapping(raw_metric)
                if metric and metric.get("fiscal_period") != quarter_period:
                    _period_basis_finding(findings, f"{base}.expectations.{e_index}.metrics.{metric_name}.fiscal_period")
        actuals = _mapping(quarter.get("actual_results"))
        if actuals and actuals.get("fiscal_period") != quarter_period:
            _period_basis_finding(findings, f"{base}.actual_results.fiscal_period")
        for metric_name, raw_metric in _mapping(actuals.get("metrics")).items():
            metric = _mapping(raw_metric)
            if metric and metric.get("fiscal_period") != quarter_period:
                _period_basis_finding(findings, f"{base}.actual_results.metrics.{metric_name}.fiscal_period")

        surprises = _mapping(quarter.get("surprise_layers"))
        eps = _mapping(surprises.get("eps_vs_sell_side"))
        if eps:
            estimate = _mapping(eps.get("estimate"))
            actual = _mapping(eps.get("actual"))
            normalized = _mapping(eps.get("normalized"))
            estimate_basis = estimate.get("accounting_basis")
            actual_basis = actual.get("accounting_basis")
            if normalized.get("status") == "computed" and (
                estimate_basis in {None, "unknown", "not_applicable"}
                or actual_basis in {None, "unknown", "not_applicable"}
                or estimate_basis != actual_basis
            ) and not eps.get("normalization_mapping_id"):
                _finding(
                    findings,
                    category=AuditCategory.GAAP_MISMATCH,
                    path=f"{base}.surprise_layers.eps_vs_sell_side",
                    failure="EPS surprise is represented as computed across unknown or unequal accounting bases without a validated normalization mapping.",
                    inference="A consumer could mistake a GAAP/non-GAAP arithmetic difference for an economically valid earnings surprise.",
                    prevention="Require equal explicit accounting bases or a receipt-backed normalization_mapping_id before normalized.status=computed.",
                    severity=AuditSeverity.CRITICAL,
                )
            if _metric_signature(estimate)[:1] != _metric_signature(actual)[:1]:
                _period_basis_finding(findings, f"{base}.surprise_layers.eps_vs_sell_side")
        for layer_name in ("vs_sell_side", "vs_prior_guidance"):
            for s_index, raw_surprise in enumerate(_sequence(surprises.get(layer_name))):
                surprise = _mapping(raw_surprise)
                if surprise.get("delta") is None:
                    continue
                if _metric_signature(_mapping(surprise.get("expected"))) != _metric_signature(_mapping(surprise.get("actual"))):
                    _period_basis_finding(findings, f"{base}.surprise_layers.{layer_name}.{s_index}")

    consensus = _mapping(card.get("consensus_state"))
    consensus_period = consensus.get("fiscal_period")
    for metric_name, raw_metric in _mapping(consensus.get("metrics")).items():
        metric = _mapping(raw_metric)
        if consensus_period and metric.get("fiscal_period") not in (None, consensus_period):
            _period_basis_finding(findings, f"consensus_state.metrics.{metric_name}.fiscal_period")


def _period_basis_finding(findings: list[AuditFinding], path: str) -> None:
    _finding(
        findings,
        category=AuditCategory.PERIOD_BASIS_MISMATCH,
        path=path,
        failure="Compared or nested metric operands do not share the declared fiscal period and complete comparison basis.",
        inference="A downstream agent could label a cross-period or cross-basis difference as a same-quarter surprise.",
        prevention="Require exact fiscal_period, accounting_basis, growth_basis, unit, and scope compatibility before computing or nesting the comparison.",
    )


def _audit_evidence(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    registry = _mapping(card.get("evidence_registry"))
    receipts = _sequence(registry.get("receipts"))
    claims = _sequence(registry.get("semantic_claims"))
    receipt_ids = {str(_mapping(item).get("id")) for item in receipts if _mapping(item).get("id")}
    claim_ids = {str(_mapping(item).get("id")) for item in claims if _mapping(item).get("id")}
    known = receipt_ids | claim_ids
    for index, raw_receipt in enumerate(receipts):
        receipt = _mapping(raw_receipt)
        for input_id in _sequence(receipt.get("input_receipt_ids")):
            if str(input_id) not in receipt_ids:
                _dangling(findings, f"evidence_registry.receipts.{index}.input_receipt_ids", str(input_id))
    for index, raw_claim in enumerate(claims):
        evidence_id = _mapping(_mapping(raw_claim).get("evidence")).get("id")
        if not evidence_id or str(evidence_id) not in receipt_ids:
            _dangling(findings, f"evidence_registry.semantic_claims.{index}.evidence.id", str(evidence_id))
    for path, key, value in _walk(card):
        if key not in {"evidence_refs", "receipt_refs", "semantic_claim_ids"} and not key.endswith("receipt_ids"):
            continue
        for reference in _sequence(value):
            if str(reference) not in known:
                _dangling(findings, path, str(reference))


def _dangling(findings: list[AuditFinding], path: str, reference: str) -> None:
    _finding(
        findings,
        category=AuditCategory.DANGLING_EVIDENCE,
        path=path,
        failure=f"Evidence reference {reference!r} does not resolve inside the canonical registry.",
        inference="A consumer could repeat an untraceable fact or decision input as if supporting evidence existed.",
        prevention="Resolve every claim, operand, and decision reference against the immutable receipt registry before publication.",
        severity=AuditSeverity.CRITICAL,
    )


def _audit_speakers(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    claims = _sequence(_mapping(card.get("evidence_registry")).get("semantic_claims"))
    for index, raw_claim in enumerate(claims):
        claim = _mapping(raw_claim)
        topics = {str(value).lower() for value in _sequence(claim.get("topics"))}
        role = str(claim.get("speaker_role", "")).lower()
        proposition = str(claim.get("proposition", ""))
        flagged = any("unresolved_management_speaker" in topic or "speaker_error" in topic for topic in topics)
        numeric_guidance = bool(re.search(r"\d", proposition)) and any("guidance" in topic for topic in topics)
        if flagged or (numeric_guidance and role in {"unknown", "unresolved"}):
            _finding(
                findings,
                category=AuditCategory.SPEAKER_ERROR,
                path=f"evidence_registry.semantic_claims.{index}",
                failure="Material numeric or guidance language has an unresolved or erroneous management speaker attribution.",
                inference="A downstream agent could treat an analyst question or unidentified remark as management guidance.",
                prevention="Require a resolved management speaker and preserved Q&A linkage for material numeric guidance claims.",
            )


def _audit_attribution(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    reactions = list(_sequence(card.get("market_reaction_history")))
    reactions.extend(
        _mapping(q).get("reaction")
        for q in _sequence(card.get("historical_quarters"))
        if _mapping(q).get("reaction") is not None
    )
    causal = re.compile(r"\b(?:caused?|because of|drove|led to|resulted in)\b", re.I)
    for r_index, raw_reaction in enumerate(reactions):
        for a_index, raw_attribution in enumerate(_sequence(_mapping(raw_reaction).get("attributions"))):
            attribution = _mapping(raw_attribution)
            path = f"market_reactions.{r_index}.attributions.{a_index}"
            if attribution.get("classification") not in (None, "inferred"):
                _causal_finding(findings, path)
            if attribution.get("dominant_driver_language") not in (None, "likely_reaction_driver"):
                _causal_finding(findings, f"{path}.dominant_driver_language")
            for c_index, raw_candidate in enumerate(_sequence(attribution.get("candidate_drivers"))):
                candidate = _mapping(raw_candidate)
                if candidate.get("evidence_class") not in {"inferred", "attributed"} or causal.search(str(candidate.get("rationale", ""))):
                    _causal_finding(findings, f"{path}.candidate_drivers.{c_index}")


def _causal_finding(findings: list[AuditFinding], path: str) -> None:
    _finding(
        findings,
        category=AuditCategory.CAUSAL_OVERSTATEMENT,
        path=path,
        failure="Reaction attribution is represented as fact or deterministic causation rather than bounded inference.",
        inference="A consumer could conclude that a measured price move proves a single causal driver.",
        prevention="Constrain attribution to inferred/attributed evidence, likely-driver language, alternatives, and window-specific confidence.",
    )


def _audit_staleness(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    statuses = {
        "identity.card.status": _mapping(_mapping(card.get("identity")).get("card")).get("status"),
        "quality_control.publication_status": _mapping(card.get("quality_control")).get("publication_status"),
    }
    for path, status in statuses.items():
        if status == "stale":
            _finding(
                findings,
                category=AuditCategory.STALE_DATA,
                path=path,
                failure="The card is explicitly marked stale.",
                inference="A downstream decision could use superseded quarter, event-date, or consensus state.",
                prevention="Regenerate and re-audit after the declared freshness trigger; never clear stale state by editing the status alone.",
            )


def _audit_precision_and_nulls(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    threshold_paths = (
        ("decision_context.entry_threshold", _mapping(_mapping(card.get("decision_context")).get("entry_threshold"))),
        ("decision.entry_threshold", _mapping(_mapping(card.get("decision")).get("entry_threshold"))),
    )
    for path, threshold in threshold_paths:
        value = threshold.get("value")
        required = ("currency", "methodology", "inputs", "computed_at", "sensitivity")
        if value is not None and any(threshold.get(name) in (None, [], (), "") for name in required):
            _finding(
                findings,
                category=AuditCategory.FALSE_PRECISION,
                path=f"{path}.value",
                failure="A precise entry threshold is present without a complete reproducible methodology, inputs, timestamp, and sensitivity.",
                inference="A consumer could treat an unexplained scalar as a validated price target.",
                prevention="Null or remove the threshold unless every deterministic derivation field and evidence input is present.",
            )
        if _is_zero(value):
            _null_zero(findings, f"{path}.value")

    reference = _mapping(_mapping(card.get("decision_context")).get("reference_price"))
    if _is_zero(reference.get("value")):
        _null_zero(findings, "decision_context.reference_price.value")
    for path, _key, value in _walk(card):
        numeric = _mapping(value)
        if "status" not in numeric or "value" not in numeric:
            continue
        status = numeric.get("status")
        number = numeric.get("value")
        value_path = f"{path}.value"
        if status in {
            "unknown",
            "not_computed",
            "insufficient_calibration",
            "removed_due_to_unverifiable_derivation",
        } and number is not None:
            _null_zero(findings, value_path)
        if status in {"known", "computed"} and number is None:
            _null_zero(findings, value_path)


def _null_zero(findings: list[AuditFinding], path: str) -> None:
    _finding(
        findings,
        category=AuditCategory.NULL_ZERO_CONFUSION,
        path=path,
        failure="A numeric zero or value/status pair substitutes for an unknown or non-computed value.",
        inference="A consumer could interpret missing evidence as an observed zero, zero price, or computed threshold.",
        prevention="Enforce status/value invariants and preserve unknown numeric values as null.",
        severity=AuditSeverity.CRITICAL,
    )


def _audit_horizons(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    decision = _mapping(card.get("decision"))
    active = decision.get("status", "not_evaluated") != "not_evaluated" or decision.get("action") is not None
    horizon = _mapping(decision.get("horizon"))
    if active and not _complete_horizon(horizon):
        _horizon_finding(findings, "decision.horizon")
    context = _mapping(card.get("decision_context"))
    starts = context.get("horizon_start")
    ends = context.get("horizon_end")
    if (starts is None) != (ends is None) or (starts is not None and ends is not None and str(ends) <= str(starts)):
        _horizon_finding(findings, "decision_context")
    long_term = _mapping(decision.get("long_term_view"))
    if long_term.get("action") is not None and not _complete_horizon(_mapping(long_term.get("horizon"))):
        _horizon_finding(findings, "decision.long_term_view.horizon")


def _horizon_finding(findings: list[AuditFinding], path: str) -> None:
    _finding(
        findings,
        category=AuditCategory.HORIZON_AMBIGUITY,
        path=path,
        failure="Decision-bearing output lacks one explicit, ordered horizon or conflates event and long-term horizons.",
        inference="A downstream agent could apply an event-trade action to an unspecified or long-term holding period.",
        prevention="Require ordered start/end timestamps and a rationale for each separately typed decision horizon.",
        severity=AuditSeverity.CRITICAL,
    )


def _audit_embedded_release_state(card: Mapping[str, Any], findings: list[AuditFinding]) -> None:
    qc = _mapping(card.get("quality_control"))
    for index, blocker in enumerate(_sequence(qc.get("blockers"))):
        _finding(
            findings,
            category=AuditCategory.RELEASE_GOVERNANCE,
            path=f"quality_control.blockers.{index}",
            failure=str(blocker),
            inference="A downstream release process could ignore a blocker embedded in the canonical card.",
            prevention="Require both embedded blockers and the current QC/audit reports to be empty before human approval.",
        )


def _metric_signature(metric: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(metric.get(key) for key in ("fiscal_period", "accounting_basis", "growth_basis", "unit", "scope"))


def _complete_horizon(horizon: Mapping[str, Any]) -> bool:
    start, end = horizon.get("start"), horizon.get("end")
    return bool(start and end and str(end) > str(start) and horizon.get("rationale"))


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _is_zero(value: object) -> bool:
    if value is None:
        return False
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False


def _walk(value: object, path: str = ""):
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, str(key), child
            yield from _walk(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            child_path = f"{path}.{index}" if path else str(index)
            yield from _walk(child, child_path)
