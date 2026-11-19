"""Deterministic key-free external agent used only for tests and demos."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal

from .external import (
    ExternalAgentBundle,
    ExternalAgentReport,
    ExternalClaim,
    ExternalRecommendation,
    ExternalScenario,
    UsageReceipt,
)
from .external_validation import evidence_id
from .models import Citation


class DeterministicExternalAgentFixture:
    provider = "fixture"
    model = "external-agent-fixture-v1"

    def interpret(self, bundle: ExternalAgentBundle) -> ExternalAgentReport:
        if not bundle.evidence:
            raise ValueError("fixture external agent requires at least one evidence passage")
        first = bundle.evidence[0]
        citation = Citation(source_id=first.source_id, passage_id=first.passage_id)
        metric_ids = (
            (bundle.canonical_metrics[0].metric_id,)
            if bundle.canonical_metrics
            else ()
        )
        generated_at = bundle.built_at.astimezone(timezone.utc)
        return ExternalAgentReport(
            report_id=f"fixture-{bundle.content_sha256[:16]}",
            bundle_id=bundle.bundle_id,
            bundle_revision=bundle.bundle_revision,
            bundle_sha256=bundle.content_sha256,
            session_id=bundle.session_id,
            event_id=bundle.event_id,
            generated_at=generated_at,
            claims=(
                ExternalClaim(
                    claim_id="fixture-claim-1",
                    text="The supplied evidence identifies a material pre-event debate.",
                    citations=(citation,),
                    confidence=Decimal("0.60"),
                ),
            ),
            scenarios=(
                ExternalScenario(
                    scenario_id="bull",
                    thesis="The cited debate resolves positively.",
                    probability=Decimal("0.25"),
                    canonical_metric_ids=metric_ids,
                    citations=(citation,),
                ),
                ExternalScenario(
                    scenario_id="base",
                    thesis="The cited debate remains balanced.",
                    probability=Decimal("0.50"),
                    canonical_metric_ids=metric_ids,
                    citations=(citation,),
                ),
                ExternalScenario(
                    scenario_id="bear",
                    thesis="The cited debate resolves negatively.",
                    probability=Decimal("0.25"),
                    canonical_metric_ids=metric_ids,
                    citations=(citation,),
                ),
            ),
            recommendation=ExternalRecommendation(
                action="HOLD",
                conviction=Decimal("0.50"),
                thesis="The fixture evidence is balanced.",
                counter_thesis="Additional evidence could change the balance.",
                supporting_evidence_ids=(evidence_id(first.source_id, first.passage_id),),
                missing_evidence=("company-specific valuation",),
                conditions_to_change=("validated material evidence changes",),
            ),
            warnings=("deterministic fixture output; not a live model result",),
            usage=UsageReceipt(
                provider=self.provider,
                model=self.model,
                prompt_version="external-agent-fixture-v1",
                started_at=generated_at,
                completed_at=generated_at,
                input_tokens=0,
                output_tokens=0,
            ),
        )


__all__ = ["DeterministicExternalAgentFixture"]
