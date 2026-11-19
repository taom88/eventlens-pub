from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.domain.events import EventTiming
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
from eventlens.product.research_evidence import (
    CompanyContextEvidence,
    ConsensusEvidence,
    EvidenceMissingness,
    EvidenceReceipt,
    HistoricalAnalyticsEvidence,
    NewsEvidence,
    ResearchEvidenceBundle,
    ResearchEvidenceStore,
    TargetEventIdentity,
    TechnicalEvidence,
    MissingnessKind,
    NewlyCoveredIssuerEligibility,
    assemble_research_evidence_bundle,
    research_evidence_json_schema,
)
from eventlens.product.news_signal import aggregate_news_signal
from eventlens.quant.backtest import event_study_report_sha256
from eventlens.quant.inputs import assemble_event_analytics_inputs, run_event_study_from_bundle
from eventlens.quant.pre_event_features import PreEventFeatureSnapshot
from eventlens.providers.fixture import FixtureProvider


CUTOFF = datetime(2026, 10, 24, 20, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def _context(*, ticker: str = "DEMO", cutoff: datetime = CUTOFF):
    source = CompanyContextSource(
        source_id="demo-10k", source_type=ContextSourceType.ISSUER_FILING,
        source_sha256="a" * 64, available_at=cutoff - timedelta(days=10),
        citation="Example Systems 2025 Form 10-K", source_url="https://example.test/demo-10k",
        citation_locator="Item 1",
    )
    statement = CitedCompanyStatement(text="Example Systems operates cloud and commerce segments.", source_ids=(source.source_id,))
    return build_company_context(
        ticker=ticker, artifact_version=1, built_at=cutoff, information_cutoff_at=cutoff,
        ontology=OntologyCatalog.packaged().resolve("sector.internet_platform"), sources=(source,),
        observations=(CompanySlotObservation(
            slot_id="identity.ticker", value=ticker, unit="text", fact_period="current",
            as_of_at=source.available_at, source_ids=(source.source_id,),
        ),),
        identity=(statement,), management=(statement,), segments=(statement,), revenue_drivers=(statement,),
        historical_overview=(statement,), target_event_id=f"{ticker}:2026-10-29:2026Q3",
        ontology_use_mode=OntologyUseMode.CURRENT_METHOD_RETROSPECTIVE,
    )


def _bundle(
    *, ticker: str = "DEMO", cutoff: datetime = CUTOFF,
    news: NewsEvidence | None = None,
) -> ResearchEvidenceBundle:
    context = _context(ticker=ticker, cutoff=cutoff)
    evidence = CompanyContextEvidence(
        context=context,
        receipt=EvidenceReceipt(
            component="company_context", receipt_id=f"company-context:{ticker.lower()}",
                content_sha256=context.content_sha256, available_at=cutoff - timedelta(microseconds=1),
            source_ref="eventlens-company-context:demo",
        ),
    )
    return assemble_research_evidence_bundle(
        bundle_id="demo-evidence-1",
        target=TargetEventIdentity(
            ticker=ticker, event_id=f"{ticker}:2026-10-29:2026Q3", fiscal_period="FY2026Q3",
            event_date=datetime(2026, 10, 29, tzinfo=timezone.utc).date(),
            release_status="confirmed", timing=EventTiming.AFTER_MARKET_CLOSE,
            market="US", exchange_calendar="XNYS", currency="USD",
            event_at=datetime(2026, 10, 29, 21, tzinfo=timezone.utc),
        ),
        information_cutoff_at=cutoff, assembled_at=cutoff, company_context=evidence,
        news=news,
        newly_covered_issuer=NewlyCoveredIssuerEligibility(
            ticker=ticker, approved_by="test-owner",
            approved_at=cutoff - timedelta(microseconds=1),
            reason="test issuer instance is explicitly newly covered",
            source_ref="test-approval:newly-covered",
        ),
        missingness=(EvidenceMissingness(
            component="prior_cards", field="__component__",
            kind=MissingnessKind.INSUFFICIENT_HISTORY,
            reason="test issuer instance is explicitly newly covered",
        ),),
    )


def _news_evidence(*, cutoff: datetime) -> NewsEvidence:
    from tests.agents.test_news import _request, _result

    signal = aggregate_news_signal((_result(_request()),), cutoff_at=cutoff)
    return _evidence_for_news_signal(signal, cutoff=cutoff)


def _mixed_news_evidence(*, cutoff: datetime) -> NewsEvidence:
    from tests.agents.test_news import _request, _result

    signal = aggregate_news_signal((
        _result(_request(ticker="DEMO", article_id="demo-news")),
        _result(_request(ticker="OTHER", article_id="other-news")),
    ), cutoff_at=cutoff)
    return _evidence_for_news_signal(signal, cutoff=cutoff)


def _evidence_for_news_signal(signal, *, cutoff: datetime) -> NewsEvidence:
    return NewsEvidence(
        signal=signal,
        receipt=EvidenceReceipt(
            component="news_signal", receipt_id="news-signal:synthetic",
            content_sha256=signal.signal_sha256 or "", available_at=cutoff - timedelta(seconds=1),
            source_ref="synthetic:news-classification",
        ),
    )


def test_research_evidence_bundle_is_canonical_and_reloads(tmp_path) -> None:
    bundle = _bundle()
    assert bundle.bundle_sha256 == bundle.content_checksum()
    assert bundle.ontology_coverage.context_content_sha256 == bundle.company_context.context.content_sha256
    path = tmp_path / "research-evidence.json"
    digest = ResearchEvidenceStore().write(bundle, path)
    assert len(digest) == 64
    assert ResearchEvidenceStore().read(path) == bundle


def test_legacy_v1_bytes_remain_checksum_readable_but_cannot_enter_v2_synthesis(tmp_path) -> None:
    legacy = _bundle().model_dump(mode="json")
    legacy["schema_version"] = "eventlens.research-evidence.v1"
    legacy.pop("newly_covered_issuer")
    legacy["bundle_sha256"] = sha256_hex({
        key: value for key, value in legacy.items() if key != "bundle_sha256"
    })
    path = tmp_path / "legacy-v1.json"
    path.write_bytes(canonical_json_bytes(legacy) + b"\n")

    assert ResearchEvidenceStore().read_legacy_v1_payload(path) == legacy
    with pytest.raises(ValueError, match="schema_version"):
        ResearchEvidenceStore().read(path)

    duplicate = tmp_path / "legacy-duplicate.json"
    duplicate.write_text(
        '{"schema_version":"eventlens.research-evidence.v1",'
        '"schema_version":"eventlens.research-evidence.v1"}\n'
    )
    with pytest.raises(ValueError, match="repeats JSON key"):
        ResearchEvidenceStore().read_legacy_v1_payload(duplicate)


def test_nested_company_context_forgery_is_rejected_even_if_outer_hash_is_recomputed() -> None:
    bundle = _bundle()
    payload = bundle.model_dump(mode="json")
    payload["company_context"]["context"]["ticker"] = "OTHER"
    payload["bundle_sha256"] = sha256_hex({key: value for key, value in payload.items() if key != "bundle_sha256"})
    with pytest.raises(ValueError, match="company-context checksum"):
        ResearchEvidenceBundle.model_validate(payload)


def test_newly_covered_eligibility_is_checksum_ticker_and_cutoff_bound() -> None:
    bundle = _bundle()
    eligibility = bundle.newly_covered_issuer
    assert eligibility is not None and eligibility.eligibility_sha256

    forged = eligibility.model_dump(mode="json")
    forged["ticker"] = "OTHER"
    with pytest.raises(ValueError, match="eligibility checksum"):
        NewlyCoveredIssuerEligibility.model_validate(forged)

    for replacement, message in (
        (NewlyCoveredIssuerEligibility(
            ticker="OTHER", approved_by="test-owner",
            approved_at=CUTOFF - timedelta(seconds=1), reason="new issuer",
            source_ref="test-approval:other",
        ), "ticker must match"),
        (NewlyCoveredIssuerEligibility(
            ticker="DEMO", approved_by="test-owner", approved_at=CUTOFF,
            reason="new issuer", source_ref="test-approval:late",
        ), "must precede information cutoff"),
    ):
        with pytest.raises(ValueError, match=message):
            ResearchEvidenceBundle.model_validate(
                bundle.model_copy(update={
                    "newly_covered_issuer": replacement, "bundle_sha256": None,
                }).model_dump(mode="json")
            )


def test_optional_sections_receive_typed_missingness_and_permutation_is_canonical() -> None:
    first = _bundle()
    second = assemble_research_evidence_bundle(
        bundle_id="demo-evidence-1", target=first.target,
        information_cutoff_at=CUTOFF, assembled_at=CUTOFF,
        company_context=first.company_context,
        newly_covered_issuer=first.newly_covered_issuer,
        missingness=tuple(reversed(first.missingness)),
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert {item.component for item in first.missingness} == {
        "consensus", "historical_analytics", "management", "news", "prior_cards", "technical",
    }


def test_exact_cutoff_and_dangling_provenance_are_rejected_after_rechecksum() -> None:
    bundle = _bundle()
    payload = bundle.model_dump(mode="json")
    payload["company_context"]["receipt"]["available_at"] = CUTOFF.isoformat()
    payload["bundle_sha256"] = sha256_hex({key: value for key, value in payload.items() if key != "bundle_sha256"})
    with pytest.raises(ValueError, match="strictly before"):
        ResearchEvidenceBundle.model_validate(payload)

    payload = bundle.model_dump(mode="json")
    payload["company_context"]["receipt"]["input_receipt_ids"] = ["missing-receipt"]
    payload["bundle_sha256"] = sha256_hex({key: value for key, value in payload.items() if key != "bundle_sha256"})
    with pytest.raises(ValueError, match="dangling input provenance"):
        ResearchEvidenceBundle.model_validate(payload)


def test_news_classification_ticker_must_match_target_with_matching_control() -> None:
    from tests.agents.test_news import CUTOFF as NEWS_CUTOFF

    signal = _news_evidence(cutoff=NEWS_CUTOFF)
    matching = _bundle(ticker="DEMO", cutoff=NEWS_CUTOFF, news=signal)
    assert matching.target.ticker == "DEMO"
    assert matching.news is not None
    assert all(item.request.ticker == matching.target.ticker for item in matching.news.signal.classifications)

    with pytest.raises(ValueError, match="classification ticker mismatches target bundle"):
        _bundle(ticker="OTHER", cutoff=NEWS_CUTOFF, news=signal)

    with pytest.raises(ValueError, match="classification ticker mismatches target bundle"):
        _bundle(ticker="DEMO", cutoff=NEWS_CUTOFF, news=_mixed_news_evidence(cutoff=NEWS_CUTOFF))

    serialized = _bundle(ticker="DEMO", cutoff=NEWS_CUTOFF, news=signal).model_dump(mode="json")
    serialized["news"] = _mixed_news_evidence(cutoff=NEWS_CUTOFF).model_dump(mode="json")
    serialized["bundle_sha256"] = sha256_hex({
        key: value for key, value in serialized.items() if key != "bundle_sha256"
    })
    with pytest.raises(ValueError, match="classification ticker mismatches target bundle"):
        ResearchEvidenceBundle.model_validate(serialized)


def test_technical_adapter_excludes_legacy_news_and_target_outcome_fields() -> None:
    snapshot = PreEventFeatureSnapshot(
        ticker="DEMO", event_id="DEMO:2026-10-29:2026Q3", event_date=datetime(2026, 10, 29).date(),
        cutoff_at=CUTOFF, article_count=9, source_count=3, topic_counts={"earnings": 9},
        weighted_sentiment=None, latest_close=None, prior_5d_return_pct=None, prior_20d_return_pct=None,
    )
    receipt = EvidenceReceipt(
        component="pre_event_features", receipt_id="technical:demo",
        content_sha256=sha256_hex(snapshot.model_dump(mode="json")),
        available_at=CUTOFF - timedelta(seconds=1), source_ref="fixture:technical",
    )
    technical = TechnicalEvidence.from_snapshot(snapshot, receipt=receipt)
    assert "weighted_sentiment" not in technical.technical.model_dump()
    assert "article_count" not in technical.technical.model_dump()

    leaked = snapshot.model_copy(update={"realized_outcome_status": "attached"})
    with pytest.raises(ValueError, match="target outcomes"):
        TechnicalEvidence.from_snapshot(leaked, receipt=receipt)

    forged = technical.model_dump(mode="json")
    forged["technical"]["latest_close"] = "999"
    with pytest.raises(ValueError, match="derived exactly"):
        TechnicalEvidence.model_validate(forged)

    different_snapshot = snapshot.model_copy(update={"event_id": "DEMO:2026-11-01:2026Q4"})
    different_receipt = receipt.model_copy(update={
        "receipt_id": "technical:other",
        "content_sha256": sha256_hex(different_snapshot.model_dump(mode="json")),
    })
    with pytest.raises(ValueError, match="technical projection identity"):
        assemble_research_evidence_bundle(
            bundle_id="technical-mismatch", target=_bundle().target,
            information_cutoff_at=CUTOFF, assembled_at=CUTOFF,
            company_context=_bundle().company_context,
            technical=TechnicalEvidence.from_snapshot(different_snapshot, receipt=different_receipt),
        )


def test_schema_is_checked_in_and_rechecksummed_missingness_forgery_fails() -> None:
    assert json.loads((ROOT / "schemas/eventlens.research_evidence.v2.schema.json").read_text()) == research_evidence_json_schema()
    payload = _bundle().model_dump(mode="json")
    payload["missingness"].append(payload["missingness"][0])
    payload["bundle_sha256"] = sha256_hex({key: value for key, value in payload.items() if key != "bundle_sha256"})
    with pytest.raises(ValueError, match="sorted and unique"):
        ResearchEvidenceBundle.model_validate(payload)


def test_historical_receipt_binds_distinct_manifest_and_report_receipts() -> None:
    provider = FixtureProvider(ROOT / "fixtures")
    analytics = assemble_event_analytics_inputs(
        provider.get_earnings_events("DEMO", datetime(2020, 1, 1).date(), datetime(2030, 1, 1).date()),
        provider.get_daily_history("DEMO", datetime(2020, 1, 1).date(), datetime(2030, 1, 1).date()),
        information_cutoff_at=datetime(2026, 1, 6, 21, tzinfo=timezone.utc), allow_degraded=True,
    )
    report, manifest = run_event_study_from_bundle(analytics)
    manifest_sha = sha256_hex(manifest.model_dump(mode="json"))
    report_sha = event_study_report_sha256(report)
    combined = sha256_hex({"manifest": manifest_sha, "report": report_sha, "supplement": None})
    receipt = EvidenceReceipt(
        component="historical_analytics", receipt_id="analytics:demo", content_sha256=combined,
        available_at=CUTOFF - timedelta(days=1), source_ref="fixture:analytics", quality="degraded",
    )
    evidence = HistoricalAnalyticsEvidence(
        input_manifest=manifest, event_study=report, manifest_sha256=manifest_sha,
        report_sha256=report_sha, receipt=receipt,
    )
    assert evidence.report_sha256 == report_sha
    with pytest.raises(ValueError, match="component checksums"):
        HistoricalAnalyticsEvidence(
            input_manifest=manifest, event_study=report, manifest_sha256=manifest_sha,
            report_sha256="0" * 64, receipt=receipt,
        )

    reordered = report.model_copy(update={"observations": tuple(reversed(report.observations))})
    reordered_sha = event_study_report_sha256(reordered)
    reordered_receipt = receipt.model_copy(update={
        "content_sha256": sha256_hex({"manifest": manifest_sha, "report": reordered_sha, "supplement": None}),
    })
    with pytest.raises(ValueError, match="event-study observations"):
        HistoricalAnalyticsEvidence(
            input_manifest=manifest, event_study=reordered, manifest_sha256=manifest_sha,
            report_sha256=reordered_sha, receipt=reordered_receipt,
        )

    with pytest.raises(ValueError, match="receipt quality"):
        HistoricalAnalyticsEvidence(
            input_manifest=manifest, event_study=report, manifest_sha256=manifest_sha,
            report_sha256=report_sha, receipt=receipt.model_copy(update={"quality": "complete"}),
        )
