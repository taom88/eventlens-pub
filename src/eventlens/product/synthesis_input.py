"""Universal research-evidence input for every product synthesis route."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from eventlens.agents import (
    Capability,
    CanonicalMetric,
    ExternalEvidence,
    HistoricalContextRef,
    assemble_external_bundle,
)
from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.config import AgentRoute
from eventlens.context_cards.rendering import render_context_card_markdown

from .research_evidence import ResearchEvidenceBundle


class ResearchEvidenceSource(Protocol):
    """Issuer-instance boundary; central orchestration never branches on ticker."""

    def build(self, *, ticker: str, question: str, session_id: str) -> ResearchEvidenceBundle: ...


def _typed_summary(value: object) -> str:
    summary = canonical_json_bytes(value).decode("utf-8")
    if len(summary) > 2_000:
        raise ValueError("projected evidence summary exceeds external-agent bound")
    return summary


def _summary_evidence(
    *, receipt, passage_id: str, evidence_kind: str, value: object,
    rights_status: str = "summary_permitted", retention_status: str = "summary_only",
) -> ExternalEvidence:
    summary = _typed_summary(value)
    return ExternalEvidence(
        source_id=receipt.receipt_id,
        passage_id=passage_id,
        evidence_kind=evidence_kind,
        typed_summary=summary,
        available_at=receipt.available_at,
        content_sha256=sha256_hex(summary),
        rights_status=rights_status,
        retention_status=retention_status,
        provenance_ref=receipt.source_ref,
    )


def _metric(
    *, metric_id: str, value: Decimal | int | None, unit: str, period: str,
    as_of: datetime, calculation_version: str, provenance_ids: tuple[str, ...],
    missing_reason: str = "not available",
) -> CanonicalMetric:
    return CanonicalMetric(
        metric_id=metric_id,
        value=Decimal(value) if isinstance(value, int) else value,
        missing_reason=None if value is not None else missing_reason,
        unit=unit,
        period=period,
        as_of=as_of,
        calculation_version=calculation_version,
        provenance_ids=provenance_ids,
    )


def external_bundle_from_research_evidence(
    evidence_bundle: ResearchEvidenceBundle,
    *,
    question: str,
    session_id: str,
    route: AgentRoute,
    built_at: datetime | None = None,
) :
    """Project one validated facts bundle into the shared agent contract.

    This is deliberately a projection, not another collection step.  It accepts
    no providers and revalidates the complete canonical bundle before exposing
    bounded summaries, metrics, and historical-card references.
    """

    facts = ResearchEvidenceBundle.model_validate(
        evidence_bundle.model_dump(mode="json")
    )
    if session_id.strip() == "":
        raise ValueError("session_id must not be blank")
    assembled_at = built_at or datetime.now(timezone.utc)
    if assembled_at.tzinfo is None or assembled_at.utcoffset() is None:
        raise ValueError("agent bundle build timestamp must be timezone-aware")

    external_evidence: list[ExternalEvidence] = []
    context = facts.company_context.context
    source_by_id = {item.source_id: item for item in context.sources}
    external_evidence.append(_summary_evidence(
        receipt=facts.company_context.receipt,
        passage_id="target-event-identity",
        evidence_kind="target.identity",
        value={
            "ticker": facts.target.ticker,
            "event_id": facts.target.event_id,
            "fiscal_period": facts.target.fiscal_period,
            "event_date": facts.target.event_date,
            "release_status": facts.target.release_status,
            "timing": facts.target.timing.value,
            "market": facts.target.market,
            "exchange_calendar": facts.target.exchange_calendar,
            "currency": facts.target.currency,
            "event_at": facts.target.event_at,
        },
    ))
    statement_groups = (
        ("identity", context.identity),
        ("management", context.management),
        ("segments", context.segments),
        ("revenue_drivers", context.revenue_drivers),
        ("historical_overview", context.historical_overview),
    )
    for group, statements in statement_groups:
        for statement in statements:
            statement_id = sha256_hex(statement.model_dump(mode="json"))[:16]
            for source_id in statement.source_ids:
                source = source_by_id[source_id]
                external_evidence.append(ExternalEvidence(
                    source_id=source_id,
                    passage_id=f"company-context:{group}:{statement_id}",
                    evidence_kind=f"company_context.{group}",
                    typed_summary=statement.text,
                    available_at=source.available_at,
                    content_sha256=sha256_hex(statement.text),
                    rights_status="summary_permitted",
                    retention_status="summary_only",
                    provenance_ref=source.source_url,
                ))

    coverage = facts.ontology_coverage
    external_evidence.append(_summary_evidence(
        receipt=facts.company_context.receipt,
        passage_id="ontology-coverage",
        evidence_kind="ontology.coverage",
        value={
            "ontology_id": coverage.ontology_id,
            "ontology_version": coverage.ontology_version,
            "ontology_sha256": coverage.ontology_sha256,
            "ontology_use_mode": coverage.ontology_use_mode,
            "populated_slot_ids": coverage.populated_slot_ids,
            "conflicted_slot_ids": coverage.conflicted_slot_ids,
            "missing_slot_ids": coverage.missing_slot_ids,
        },
    ))

    historical = []
    for prior in facts.prior_cards:
        rendered = render_context_card_markdown(prior.card)
        truncated = len(rendered) > 4_000
        summary = (
            rendered
            if not truncated
            else rendered[:3_920].rstrip() + "\n\n[truncated to external-contract bound]"
        )
        caveats = tuple(prior.card.quality_control.blockers[:8])
        if truncated:
            caveats = (*caveats, "Readable card rendering was truncated to 4,000 characters.")
        historical.append(HistoricalContextRef(
            card_id=prior.card_id,
            event_id=prior.prior_event_id,
            event_date=prior.event_date,
            knowledge_cutoff_at=prior.knowledge_cutoff_at,
            card_sha256=prior.card_sha256,
            readable_summary=summary,
            key_messages=tuple(prior.card.quality_control.warnings[:8]),
            caveats=caveats,
        ))

    metrics: list[CanonicalMetric] = [
        _metric(
            metric_id="ontology.critical_slots_populated",
            value=coverage.critical_slots_populated,
            unit="count",
            period=facts.information_cutoff_at.date().isoformat(),
            as_of=facts.company_context.receipt.available_at,
            calculation_version=coverage.ontology_version,
            provenance_ids=(facts.company_context.receipt.receipt_id,),
        ),
        _metric(
            metric_id="ontology.critical_slots_total",
            value=coverage.critical_slots_total,
            unit="count",
            period=facts.information_cutoff_at.date().isoformat(),
            as_of=facts.company_context.receipt.available_at,
            calculation_version=coverage.ontology_version,
            provenance_ids=(facts.company_context.receipt.receipt_id,),
        ),
    ]
    if facts.consensus is not None:
        consensus = facts.consensus
        external_evidence.append(_summary_evidence(
            receipt=consensus.receipt,
            passage_id="consensus-state",
            evidence_kind="consensus.state",
            value={
                "availability_status": consensus.availability_status,
                "fiscal_period": consensus.consensus.fiscal_period,
                "blockers": consensus.consensus.blockers,
                "evidence_refs": consensus.evidence_refs,
            },
        ))
        for name, value in sorted(consensus.consensus.metrics.items(), key=lambda item: item[0].value):
            external_evidence.append(_summary_evidence(
                receipt=consensus.receipt,
                passage_id=f"consensus-metric:{name.value}",
                evidence_kind="consensus.metric",
                value=value.model_dump(mode="json"),
            ))
            metrics.append(_metric(
                metric_id=f"consensus.{name.value}",
                value=value.value,
                unit=value.unit.value,
                period=value.fiscal_period.label,
                as_of=consensus.receipt.available_at,
                calculation_version="research-evidence-consensus.v1",
                provenance_ids=(consensus.receipt.receipt_id, *consensus.evidence_refs),
                missing_reason="consensus metric unavailable",
            ))

    if facts.management is not None:
        management = facts.management
        for entry in management.guidance:
            guidance = entry.guidance
            external_evidence.append(_summary_evidence(
                receipt=management.receipt,
                passage_id=f"guidance:{entry.guidance_id}",
                evidence_kind="management.guidance",
                value={
                    "guidance_id": entry.guidance_id,
                    "metric": entry.metric.value,
                    "issued_fiscal_period": entry.issued_fiscal_period.label,
                    "target_fiscal_period": entry.target_fiscal_period.label,
                    "type": guidance.type.value,
                    "low": guidance.low,
                    "high": guidance.high,
                    "point": guidance.point,
                    "qualitative_statement": guidance.qualitative_statement,
                    "unit": guidance.unit.value,
                    "accounting_basis": guidance.accounting_basis.value,
                    "growth_basis": guidance.growth_basis.value,
                    "scope": guidance.scope.value,
                    "evidence_refs": entry.evidence_refs,
                },
            ))
            for bound, value in (
                ("low", guidance.low), ("high", guidance.high),
                ("point", guidance.point), ("midpoint", guidance.midpoint),
            ):
                if value is not None:
                    metrics.append(_metric(
                        metric_id=f"management_guidance.{entry.guidance_id}.{bound}",
                        value=value,
                        unit=guidance.unit.value,
                        period=entry.target_fiscal_period.label,
                        as_of=management.receipt.available_at,
                        calculation_version="research-evidence-guidance.v1",
                        provenance_ids=(management.receipt.receipt_id, *entry.evidence_refs),
                    ))
    if facts.technical is not None:
        technical = facts.technical.technical
        for name, value, unit in (
            ("latest_close", technical.latest_close, facts.target.currency),
            ("prior_5d_return_pct", technical.prior_5d_return_pct, "percent"),
            ("prior_20d_return_pct", technical.prior_20d_return_pct, "percent"),
        ):
            metrics.append(CanonicalMetric(
                metric_id=f"technical.{name}", value=value,
                missing_reason=None if value is not None else "not available",
                unit=unit, period=technical.cutoff_at.date().isoformat(),
                as_of=technical.cutoff_at,
                calculation_version=str(
                    facts.technical.snapshot.provenance_manifest.get(
                        "calculation_version", "pre-event-features-v1"
                    )
                ),
                provenance_ids=(facts.technical.receipt.receipt_id,),
            ))
    if facts.news is not None:
        signal = facts.news.signal
        external_evidence.append(_summary_evidence(
            receipt=facts.news.receipt,
            passage_id="news-signal:aggregate",
            evidence_kind="news.aggregate",
            value={
                "coverage_quality": signal.coverage_quality.value,
                "calculation_version": signal.calculation_version,
                "policy": signal.policy.model_dump(mode="json"),
                "topic_distribution": tuple(
                    item.model_dump(mode="json") for item in signal.topic_distribution
                ),
                "input_receipts": tuple(
                    item.model_dump(mode="json") for item in signal.input_receipts
                ),
            },
        ))
        for item in signal.items:
            external_evidence.append(_summary_evidence(
                receipt=facts.news.receipt,
                passage_id=f"news-signal:item:{item.article_id}",
                evidence_kind="news.item",
                value=item.model_dump(mode="json"),
            ))
        for name in (
            "weighted_content_score", "weighted_headline_score",
            "weighted_provider_native_score", "positive_share", "negative_share",
            "mixed_share", "contradiction_score", "abstention_rate",
        ):
            value = getattr(signal, name)
            metrics.append(_metric(
                metric_id=f"news.{name}", value=value,
                unit="ratio", period=signal.cutoff_at.date().isoformat(),
                as_of=signal.cutoff_at,
                calculation_version=signal.calculation_version,
                provenance_ids=(facts.news.receipt.receipt_id,),
            ))
        for name in ("material_item_count", "source_count", "duplicate_adjusted_breadth"):
            metrics.append(_metric(
                metric_id=f"news.{name}", value=getattr(signal, name), unit="count",
                period=signal.cutoff_at.date().isoformat(), as_of=signal.cutoff_at,
                calculation_version=signal.calculation_version,
                provenance_ids=(facts.news.receipt.receipt_id,),
            ))
        for result in signal.classifications:
            article = result.request.article
            content = result.request.content_evidence
            headline_only = content.status.value == "headline_only"
            derived_rights = "metadata_only" if headline_only else "summary_permitted"
            derived_retention = "headline_only" if headline_only else "summary_only"
            provider_summary_id = None
            if result.request.provider_summary_sha256 is not None:
                provider_summary_id = (
                    f"news:{article.provider_article_id}:provider-summary:"
                    f"{result.request.provider_summary_sha256}"
                )
                external_evidence.append(_summary_evidence(
                    receipt=facts.news.receipt,
                    passage_id=provider_summary_id,
                    evidence_kind="news.provider_summary",
                    value={
                        "summary": article.provider_summary,
                        "summary_sha256": result.request.provider_summary_sha256,
                    },
                ))
            for passage in result.request.passage_selection.selected_passages:
                chunks = tuple(
                    passage.excerpt[index:index + 2_000]
                    for index in range(0, len(passage.excerpt), 2_000)
                )
                for index, chunk in enumerate(chunks, start=1):
                    external_evidence.append(ExternalEvidence(
                        source_id=facts.news.receipt.receipt_id,
                        passage_id=(
                            f"news:{article.provider_article_id}:passage:"
                            f"{passage.paragraph_id}:part:{index}"
                        ),
                        evidence_kind="news.selected_passage",
                        text=chunk,
                        available_at=facts.news.receipt.available_at,
                        content_sha256=sha256_hex(chunk),
                        rights_status="retention_permitted",
                        retention_status="bounded_excerpt",
                        provenance_ref=article.url,
                    ))
            external_evidence.append(_summary_evidence(
                receipt=facts.news.receipt,
                passage_id=f"news:{article.provider_article_id}",
                evidence_kind="news.classification",
                value={
                    "article_id": article.provider_article_id,
                    "headline": article.headline,
                    "url": article.url,
                    "published_at": article.published_at,
                    "entity_match": result.entity_match.value,
                    "event_relationship": result.event_relationship.value,
                    "topics": tuple(item.value for item in result.topics),
                    "relevance": result.relevance.value,
                    "materiality": result.materiality.value,
                    "claim_ids": tuple(item.claim_id for item in result.cited_claims),
                    "headline_tone": result.agent_headline_tone.value,
                    "content_tone": result.agent_content_tone.value if result.agent_content_tone else None,
                    "fundamental_implication": (
                        result.agent_fundamental_implication.value
                        if result.agent_fundamental_implication else None
                    ),
                    "market_implication": (
                        result.agent_near_term_market_implication.value
                        if result.agent_near_term_market_implication else None
                    ),
                    "horizon": result.horizon.value if result.horizon else None,
                    "novelty": result.novelty.value,
                    "confidence": result.confidence,
                    "abstention_reason": result.abstention_reason,
                    "content_status": content.status.value,
                    "content_reason": content.reason.value,
                    "content_rights_status": content.rights_status.value,
                    "content_retention_policy": content.retention_policy.value,
                },
                rights_status=derived_rights,
                retention_status=derived_retention,
            ))
            for claim in result.cited_claims:
                projected_citations = tuple(
                    f"news:{article.provider_article_id}:passage:"
                    f"{citation.passage_id}:part:1"
                    for citation in claim.citations
                ) + ((provider_summary_id,) if claim.summary_citations else ())
                external_evidence.append(_summary_evidence(
                    receipt=facts.news.receipt,
                    passage_id=f"news:{article.provider_article_id}:claim:{claim.claim_id}",
                    evidence_kind="news.claim",
                    value={
                        "claim_id": claim.claim_id,
                        "text": claim.text,
                        "confidence": claim.confidence,
                        "projected_citation_ids": projected_citations,
                    },
                    rights_status=derived_rights,
                    retention_status=derived_retention,
                ))

    if facts.historical_analytics is not None:
        analytics = facts.historical_analytics
        report = analytics.event_study
        external_evidence.append(_summary_evidence(
            receipt=analytics.receipt,
            passage_id="historical-analytics:manifest",
            evidence_kind="historical_analytics.manifest",
            value={
                "event_ids": analytics.input_manifest.event_ids,
                "eligibility": analytics.input_manifest.eligibility.value,
                "eligibility_reasons": analytics.input_manifest.eligibility_reasons,
                "policy_version": analytics.input_manifest.policy_version,
                "event_count": report.event_count,
                "comparable_count": report.comparable_count,
                "minimum_ci_observations": report.minimum_ci_observations,
                "calculation_method": report.calculation_method,
                "recommendation_backtest_status": report.recommendation_backtest_status,
            },
        ))
        metrics.extend((
            _metric(
                metric_id="historical_analytics.event_count", value=report.event_count,
                unit="count", period="prior_events", as_of=analytics.receipt.available_at,
                calculation_version=report.calculation_version,
                provenance_ids=(analytics.receipt.receipt_id,),
            ),
            _metric(
                metric_id="historical_analytics.comparable_count", value=report.comparable_count,
                unit="count", period="prior_events", as_of=analytics.receipt.available_at,
                calculation_version=report.calculation_version,
                provenance_ids=(analytics.receipt.receipt_id,),
            ),
        ))
        for label, summary in (
            ("next_open", report.next_open_summary),
            ("t_plus_1", report.t_plus_1_summary),
            ("t_plus_5", report.t_plus_5_summary),
        ):
            external_evidence.append(_summary_evidence(
                receipt=analytics.receipt,
                passage_id=f"historical-analytics:summary:{label}",
                evidence_kind="historical_analytics.summary",
                value=summary.model_dump(mode="json"),
            ))
            statistics = summary.statistics
            for name in ("mean_pct", "median_pct", "sample_stddev_pct", "min_pct", "max_pct", "positive_rate"):
                metrics.append(_metric(
                    metric_id=f"historical_analytics.{label}.{name}",
                    value=getattr(statistics, name),
                    unit="ratio" if name == "positive_rate" else "percent",
                    period="prior_events", as_of=analytics.receipt.available_at,
                    calculation_version=report.calculation_version,
                    provenance_ids=(analytics.receipt.receipt_id,),
                ))
        for observation in report.observations:
            external_evidence.append(_summary_evidence(
                receipt=analytics.receipt,
                passage_id=f"historical-analytics:event:{observation.event_id}",
                evidence_kind="historical_analytics.observation",
                value=observation.model_dump(mode="json"),
            ))
        if analytics.supplement is not None:
            supplement = analytics.supplement
            for section, value in (
                ("metadata", {
                    "base_report_sha256": supplement.base_report_sha256,
                    "input_sha256": supplement.input_sha256,
                    "cost_policy_version": supplement.cost_policy_version,
                    "warnings": supplement.warnings,
                    "execution_status": supplement.execution_status,
                }),
                ("benchmark", supplement.benchmark.model_dump(mode="json")),
                ("t_plus_1_costs", supplement.t_plus_1_costs.model_dump(mode="json")),
                ("t_plus_5_costs", supplement.t_plus_5_costs.model_dump(mode="json")),
            ):
                external_evidence.append(_summary_evidence(
                    receipt=analytics.receipt,
                    passage_id=f"historical-analytics:supplement:{section}",
                    evidence_kind=f"historical_analytics.supplement.{section}",
                    value=value,
                ))

    missingness = tuple(
        f"{item.component}.{item.field}: {item.kind.value}: {item.reason}"
        for item in facts.missingness
    )
    unavailable = tuple(sorted({
        item.component for item in facts.missingness if item.field == "__component__"
    }))
    return assemble_external_bundle(
        bundle_id=f"{session_id}:external-agent:r1",
        session_id=session_id,
        event_id=facts.target.event_id,
        ticker=facts.target.ticker,
        route=route,
        bundle_revision=1,
        built_at=assembled_at.astimezone(timezone.utc),
        information_cutoff_at=facts.information_cutoff_at,
        target_release_at=facts.target.event_at,
        target_release_status=facts.target.release_status,
        research_question=question.strip(),
        instructions=(
            "Separate canonical facts, implications, uncertainty, and missing evidence.",
            "Keep the reaction forecast separate from any decision-policy output.",
            "No operator decision policy is supplied in this contract; abstain from an action label.",
            "Cite the exported evidence and historical context cards.",
        ),
        canonical_metrics=tuple(metrics),
        evidence=tuple(external_evidence),
        historical_context=tuple(historical),
        missingness=missingness,
        unavailable_stages=unavailable,
        warnings=tuple(sorted({warning for receipt in _receipts(facts) for warning in receipt.warnings})),
        allowed_skills=(Capability(capability_id="decision_synthesis", version="1"),),
        allowed_tools=(),
        forbidden_behaviors=(
            "change canonical metrics", "use post-cutoff evidence",
            "provide execution instructions", "treat mechanical guardrails as valuation",
        ),
    )


def _receipts(bundle: ResearchEvidenceBundle):
    receipts = [bundle.company_context.receipt, *(item.receipt for item in bundle.prior_cards)]
    for component in (bundle.consensus, bundle.management, bundle.news, bundle.technical, bundle.historical_analytics):
        if component is not None:
            receipts.append(component.receipt)
    return tuple(receipts)


__all__ = [
    "ResearchEvidenceSource",
    "external_bundle_from_research_evidence",
]
