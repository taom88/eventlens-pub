"""Pure assembly of a canonical bundle from retained live-provider receipts."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Literal

from eventlens.artifacts.models import sha256_hex
from eventlens.context_cards.accounting import ConsensusEps
from eventlens.context_cards.metrics import (
    AccountingBasis,
    FiscalPeriod,
    GrowthBasis,
    MetricScope,
    MetricUnit,
    MetricValue,
    NormalizedMetric,
)
from eventlens.context_cards.models import ConsensusState
from eventlens.ontologies import OntologyCatalog
from eventlens.product.company_context import (
    CitedCompanyStatement,
    CompanyContextSource,
    CompanySlotObservation,
    ContextSourceType,
    OntologyUseMode,
    build_company_context,
)
from eventlens.providers.models import CompanyNewsMetadata, EarningsEventRecord
from eventlens.quant.pre_event_features import build_pre_event_features

from .live_evidence import LiveEvidenceContract, RetainedLiveEvidence
from .live_evidence_collection import select_target_event
from .live_news_evidence import LiveNewsEvidenceManifest
from .research_evidence import (
    CompanyContextEvidence,
    ConsensusEvidence,
    EvidenceMissingness,
    EvidenceReceipt,
    MissingnessKind,
    ResearchEvidenceBundle,
    TargetEventIdentity,
    TechnicalEvidence,
    assemble_research_evidence_bundle,
)


def assemble_live_research_evidence(
    contract: LiveEvidenceContract,
    retained: RetainedLiveEvidence,
    news_evidence: LiveNewsEvidenceManifest,
    *,
    assembled_at: datetime,
    assembly_mode: Literal["live", "retrospective_replay"] = "live",
) -> ResearchEvidenceBundle:
    if assembly_mode == "live":
        assembled = contract.require_assembly_window(assembled_at)
    elif assembly_mode == "retrospective_replay":
        assembled = contract.require_replay_time(assembled_at)
    else:
        raise ValueError("unsupported evidence assembly mode")
    binding = (
        retained.manifest_id,
        retained.manifest_sha256,
        retained.ticker,
        retained.benchmark_ticker,
        retained.information_cutoff_at,
    )
    expected = (
        contract.manifest_id,
        contract.manifest_sha256,
        contract.ticker,
        contract.benchmark_ticker,
        contract.information_cutoff_at,
    )
    if binding != expected:
        raise ValueError("retained evidence does not match the acceptance contract")
    _validate_news_evidence(contract, retained, news_evidence)
    for response in (retained.security_market, retained.benchmark_market):
        if response is not None and response.data.currency != contract.currency:
            raise ValueError(
                "market history currency does not match the acceptance contract"
            )
    target_event = select_target_event(contract, retained.event)
    target = TargetEventIdentity(
        ticker=contract.ticker,
        event_id=contract.manifest_id,
        fiscal_period=contract.fiscal_period,
        event_date=contract.event_date,
        release_status="estimated",
        timing=contract.event_timing,
        market=contract.market,
        exchange_calendar=contract.exchange_calendar,
        currency=contract.currency,
        event_at=contract.expected_release_at,
    )
    company_context = _company_context(
        contract, retained, assembled, assembly_mode=assembly_mode
    )
    consensus = _consensus(contract, retained)
    technical = _technical(contract, retained, target_event, news_evidence)
    private_news_sha = news_evidence.manifest_sha256
    public_news_sha = news_evidence.public_projection().projection_sha256
    assert private_news_sha is not None and public_news_sha is not None
    missingness = [
        EvidenceMissingness(
            component="historical_analytics",
            field="__component__",
            kind=MissingnessKind.NOT_COMPUTED,
            reason="Historical analogy inputs remain assigned to EL-005 and EL-008.",
        ),
        EvidenceMissingness(
            component="management",
            field="__component__",
            kind=MissingnessKind.NOT_COMPUTED,
            reason="Management guidance extraction is not part of live receipt collection.",
        ),
        EvidenceMissingness(
            component="news",
            field="__component__",
            kind=MissingnessKind.NOT_COMPUTED,
            reason=(
                "Semantic news classification has not run; private resolver manifest "
                f"{private_news_sha} and public projection {public_news_sha} are retained."
            ),
        ),
        EvidenceMissingness(
            component="prior_cards",
            field="__component__",
            kind=MissingnessKind.NOT_PROVIDED,
            reason=f"Three prior {contract.ticker} cards remain assigned to EL-005.",
        ),
    ]
    if consensus is None:
        missingness.append(
            EvidenceMissingness(
                component="consensus",
                field="__component__",
                kind=MissingnessKind.PROVIDER_DEGRADED,
                reason="No cutoff-safe provider EPS estimate was retained for the target event.",
            )
        )
    if technical is None:
        missingness.append(
            EvidenceMissingness(
                component="technical",
                field="__component__",
                kind=MissingnessKind.PROVIDER_DEGRADED,
                reason=f"No cutoff-safe {contract.ticker} market history was retained.",
            )
        )
    collection_sha = retained.collection_sha256
    assert collection_sha is not None
    return assemble_research_evidence_bundle(
        bundle_id=(
            f"{contract.manifest_id}:{collection_sha[:16]}"
            if assembly_mode == "live"
            else f"replay:{contract.manifest_id}:{collection_sha[:16]}"
        ),
        target=target,
        information_cutoff_at=contract.information_cutoff_at,
        assembled_at=assembled,
        company_context=company_context,
        consensus=consensus,
        technical=technical,
        missingness=tuple(missingness),
    )


def _company_context(
    contract: LiveEvidenceContract,
    retained: RetainedLiveEvidence,
    assembled_at: datetime,
    *,
    assembly_mode: Literal["live", "retrospective_replay"],
) -> CompanyContextEvidence:
    manifest_source = CompanyContextSource(
        source_id="acceptance-manifest",
        source_type=ContextSourceType.ANALYST_INPUT,
        source_sha256=contract.manifest_sha256,
        available_at=datetime.combine(contract.frozen_on, time.min, tzinfo=UTC),
        citation=f"Owner-approved {contract.ticker} acceptance manifest",
        source_url=f"eventlens://acceptance/{contract.manifest_id}",
        citation_locator="issuer identity",
    )
    sources = [manifest_source]
    if retained.filings is not None:
        sources.append(
            CompanyContextSource(
                source_id="sec-submissions",
                source_type=ContextSourceType.ISSUER_FILING,
                source_sha256=retained.filings.provenance.raw_response_sha256,
                available_at=retained.filings.provenance.retrieved_at,
                citation="SEC submissions and recent filings index",
                source_url=contract.sec_submissions_url,
                citation_locator="registrant identity and recent filings",
            )
        )
    source_ids = tuple(item.source_id for item in sources)
    identity = (
        CitedCompanyStatement(
            text=f"{contract.legal_name} trades under ticker {contract.ticker}.",
            source_ids=source_ids,
        ),
    )
    observations = tuple(
        CompanySlotObservation(
            slot_id=slot_id,
            value=value,
            unit="text",
            fact_period="current",
            as_of_at=manifest_source.available_at,
            source_ids=source_ids,
        )
        for slot_id, value in (
            ("identity.company_name", contract.legal_name),
            ("identity.ticker", contract.ticker),
        )
    )
    context = build_company_context(
        ticker=contract.ticker,
        artifact_version=1,
        built_at=assembled_at,
        information_cutoff_at=contract.information_cutoff_at,
        ontology=OntologyCatalog.packaged().resolve(
            contract.ontology_id, as_of=contract.information_cutoff_at
        ),
        sources=tuple(sources),
        observations=observations,
        identity=identity,
        management=(),
        segments=(),
        revenue_drivers=(),
        historical_overview=(),
        target_event_id=contract.manifest_id,
        ontology_use_mode=(
            OntologyUseMode.LIVE_CURRENT
            if assembly_mode == "live"
            else OntologyUseMode.CURRENT_METHOD_RETROSPECTIVE
        ),
    )
    return CompanyContextEvidence(
        context=context,
        receipt=EvidenceReceipt(
            component="company_context",
            receipt_id=f"receipt:{retained.collection_id}:company-context",
            content_sha256=context.content_sha256,
            available_at=max(item.available_at for item in sources),
            computed_at=assembled_at,
            source_ref=f"acceptance-manifest:{contract.manifest_sha256}",
            quality="degraded",
            warnings=(
                ("only_identity_slots_populated",)
                if assembly_mode == "live"
                else (
                    "only_identity_slots_populated",
                    "retrospective_replay_after_target_event",
                )
            ),
            provenance_refs=tuple(sorted(item.source_sha256 for item in sources)),
        ),
    )


def _consensus(
    contract: LiveEvidenceContract, retained: RetainedLiveEvidence
) -> ConsensusEvidence | None:
    selected_response = selected_event = None
    for response in (retained.expectations, retained.event):
        if response is None:
            continue
        match = next(
            (
                item
                for item in response.data.events
                if item.event_date == contract.event_date
                and item.estimated_eps is not None
            ),
            None,
        )
        if match is not None:
            selected_response, selected_event = response, match
            break
    if selected_response is None or selected_event is None:
        return None
    estimate = MetricValue(
        metric=NormalizedMetric.EPS,
        value=selected_event.estimated_eps,
        unit=MetricUnit.USD_PER_SHARE,
        fiscal_period=_fiscal_period(
            contract.fiscal_period, contract.fiscal_period_ended
        ),
        accounting_basis=AccountingBasis.UNKNOWN,
        growth_basis=GrowthBasis.NOT_APPLICABLE,
        scope=MetricScope.CONSOLIDATED,
    )
    source_ref = (
        f"provider:{selected_response.provenance.provider_name}:"
        f"{selected_response.provenance.raw_response_sha256}"
    )
    state = ConsensusState(
        status="available",
        fiscal_period=contract.fiscal_period,
        metrics={NormalizedMetric.EPS: estimate},
        eps=ConsensusEps(
            estimate=estimate,
            provider=selected_response.provenance.provider_name,
            receipt_refs=(source_ref,),
        ),
    )
    checksum = sha256_hex(state.model_dump(mode="json"))
    return ConsensusEvidence(
        consensus=state,
        availability_status="available",
        evidence_refs=(source_ref,),
        consensus_sha256=checksum,
        receipt=EvidenceReceipt(
            component="consensus",
            receipt_id=f"receipt:{retained.collection_id}:consensus",
            content_sha256=checksum,
            available_at=selected_response.provenance.retrieved_at,
            source_ref=source_ref,
            quality=selected_response.provenance.data_quality.value,
            provenance_refs=(selected_response.provenance.request_fingerprint,),
        ),
    )


def _technical(
    contract: LiveEvidenceContract,
    retained: RetainedLiveEvidence,
    target_event: EarningsEventRecord,
    news_evidence: LiveNewsEvidenceManifest,
) -> TechnicalEvidence | None:
    if retained.security_market is None:
        return None
    news = (
        retained.news.data
        if retained.news is not None
        else CompanyNewsMetadata(ticker=contract.ticker, articles=())
    )
    event = target_event.model_copy(
        update={
            "source_event_id": contract.manifest_id,
            "event_release_at": contract.expected_release_at,
            "timing": contract.event_timing,
        }
    )
    snapshot = build_pre_event_features(
        event,
        news=news,
        market=retained.security_market.data,
        cutoff_at=contract.information_cutoff_at,
    )
    provenance_refs = [
        retained.security_market.provenance.request_fingerprint,
        retained.security_market.provenance.raw_response_sha256,
    ]
    available_at = retained.security_market.provenance.retrieved_at
    if retained.news is not None:
        provenance_refs += [
            retained.news.provenance.request_fingerprint,
            retained.news.provenance.raw_response_sha256,
        ]
        available_at = max(available_at, retained.news.provenance.retrieved_at)
    snapshot = snapshot.model_copy(
        update={
            "provenance_manifest": {
                "collection_sha256": retained.collection_sha256,
                "private_news_manifest_sha256": news_evidence.manifest_sha256,
                "public_news_projection_sha256": news_evidence.public_projection().projection_sha256,
                "provider_response_shas": tuple(sorted(provenance_refs)),
            }
        }
    )
    checksum = sha256_hex(snapshot.model_dump(mode="json"))
    return TechnicalEvidence.from_snapshot(
        snapshot,
        receipt=EvidenceReceipt(
            component="pre_event_features",
            receipt_id=f"receipt:{retained.collection_id}:technical",
            content_sha256=checksum,
            available_at=available_at,
            source_ref=f"live-evidence:{retained.collection_sha256}",
            quality="complete",
            provenance_refs=tuple(sorted(set(provenance_refs))),
        ),
    )


def _validate_news_evidence(
    contract: LiveEvidenceContract,
    retained: RetainedLiveEvidence,
    manifest: LiveNewsEvidenceManifest,
) -> None:
    expected_response_sha = (
        retained.news.provenance.raw_response_sha256
        if retained.news is not None
        else None
    )
    if (
        manifest.acceptance_manifest_sha256 != contract.manifest_sha256
        or manifest.ticker != contract.ticker
        or manifest.cutoff_at != contract.information_cutoff_at
        or manifest.collected_at != retained.collected_at
        or manifest.provider_response_sha256 != expected_response_sha
    ):
        raise ValueError("private news evidence does not match retained live receipts")
    retained_ids = tuple(
        sorted(
            item.provider_article_id
            for item in (() if retained.news is None else retained.news.data.articles)
        )
    )
    manifest_ids = tuple(item.article.provider_article_id for item in manifest.items)
    if manifest_ids != retained_ids:
        raise ValueError("private news evidence must cover every retained news item")


def _fiscal_period(value: str, period_end: date) -> FiscalPeriod:
    year_text, quarter_text = value.upper().removeprefix("FY").split("Q", 1)
    return FiscalPeriod(
        fiscal_year=int(year_text),
        fiscal_quarter=int(quarter_text),
        period_end=period_end,
    )


__all__ = ["assemble_live_research_evidence"]
