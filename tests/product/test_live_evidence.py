from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from eventlens.domain.events import EventTiming
from eventlens.product.live_evidence import (
    IssuerTimingRevalidation,
    IssuerTimingSource,
    RetainedLiveEvidence,
    load_live_evidence_contract,
)
from eventlens.product.live_evidence_assembly import assemble_live_research_evidence
from eventlens.product.live_evidence_collection import (
    collect_live_evidence,
    select_target_event,
)
from eventlens.product.live_evidence_store import (
    LiveEvidenceStore,
    LiveNewsEvidenceStore,
)
from eventlens.product.live_news_evidence import (
    LiveNewsEvidenceManifest,
    build_live_news_evidence,
    build_news_evidence_manifest,
    select_provider_summary_references,
)
from eventlens.product.research_evidence import ResearchEvidenceStore
from eventlens.providers.content import (
    ContentFetchPolicy,
    ContentHttpResponse,
    ContentRetentionPolicy,
    ContentSourceKind,
    RightsAwareContentFetcher,
)
from eventlens.providers.models import (
    CompanyNewsMetadata,
    DailyBar,
    DataQualityStatus,
    EarningsEventRecord,
    EarningsEvents,
    FilingEvidence,
    MarketHistory,
    NewsArticleMetadata,
    NewsSummaryStatus,
    ProviderOrigin,
    ProviderProvenance,
    ProviderResponse,
)
from eventlens.providers.registry import FreeFirstProviderRegistry

MANIFEST = Path("docs/acceptance/synthetic-after-market-event.json")
RUN_CONFIG = Path("tests/fixtures/live_run_configs/synthetic-after-market.json")
RETRIEVED_AT = datetime(2026, 11, 17, 20, 50, tzinfo=UTC)
ASSEMBLED_AT = datetime(2026, 11, 17, 21, 0, tzinfo=UTC)


def _contract(*, authorized: bool = True):
    contract = load_live_evidence_contract(MANIFEST, RUN_CONFIG)
    return contract.model_copy(update={"live_provider_run_authorized": authorized})


def _provenance(provider: str, *, origin: ProviderOrigin = ProviderOrigin.LIVE):
    digest = (provider.encode().hex() * 64)[:64].ljust(64, "0")
    return ProviderProvenance(
        provider_name=provider,
        provider_version="test-v1",
        origin=origin,
        retrieved_at=RETRIEVED_AT,
        request_parameters={"ticker": "DEMO"},
        request_fingerprint=digest,
        raw_response_sha256=hashlib_sha(f"raw:{provider}"),
        coverage_start=date(2026, 11, 1),
        coverage_end=date(2026, 11, 17),
        quota_consumed=provider in {"finnhub", "alpha_vantage"},
        data_quality=DataQualityStatus.COMPLETE,
    )


def hashlib_sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _events(provider: str = "finnhub", *, reported_eps=None):
    return ProviderResponse(
        provenance=_provenance(provider),
        data=EarningsEvents(
            ticker="DEMO",
            events=(
                EarningsEventRecord(
                    ticker="DEMO",
                    event_date=date(2026, 11, 17),
                    fiscal_quarter="2027Q3",
                    fiscal_period_end=date(2026, 10, 25),
                    timing=EventTiming.AFTER_MARKET_CLOSE,
                    source_event_id="DEMO:2026-11-17:2027Q3",
                    reported_eps=reported_eps,
                    estimated_eps=Decimal("1.43"),
                ),
            ),
        ),
    )


def _market(ticker: str):
    bars = tuple(
        DailyBar(
            trading_date=date(2026, 10, 17) + timedelta(days=index),
            open=Decimal(180) + index,
            high=Decimal(182) + index,
            low=Decimal(179) + index,
            close=Decimal(181) + index,
            adjusted_close=Decimal(181) + index,
            volume=1_000_000 + index,
        )
        for index in range(30)
    )
    return ProviderResponse(
        provenance=_provenance(f"yfinance-{ticker.lower()}"),
        data=MarketHistory(ticker=ticker, bars=bars),
    )


def _timing(contract):
    urls = tuple(
        sorted(
            (
                "https://example.com/issuer/source-1",
                "https://example.com/issuer/source-2",
            )
        )
    )
    return IssuerTimingRevalidation(
        manifest_sha256=contract.manifest_sha256,
        checked_at=RETRIEVED_AT,
        event_date=contract.event_date,
        expected_release_at=contract.expected_release_at,
        conference_call_at=contract.conference_call_at,
        sources=tuple(
            IssuerTimingSource(url=url, response_sha256=hashlib_sha(url))
            for url in urls
        ),
    )


class _Finnhub:
    def __init__(self, calls):
        self.calls = calls

    def get_earnings_events(self, ticker, start, end):
        self.calls.append(("finnhub_events", ticker, start, end))
        return _events()

    def get_company_news(self, ticker, start, end, *, cutoff_at=None):
        self.calls.append(("finnhub_news", ticker, start, end, cutoff_at))
        return ProviderResponse(
            provenance=_provenance("finnhub-news"),
            data=CompanyNewsMetadata(ticker=ticker, articles=()),
        )


class _Alpha:
    def __init__(self, calls):
        self.calls = calls

    def get_earnings_events(self, ticker, start, end):
        self.calls.append(("alpha_events", ticker, start, end))
        return _events("alpha_vantage")

    def get_company_news(self, ticker, start, end, *, cutoff_at=None):
        self.calls.append(("alpha_news", ticker, start, end, cutoff_at))
        return ProviderResponse(
            provenance=_provenance("alpha-news"),
            data=CompanyNewsMetadata(ticker=ticker, articles=()),
        )


class _Massive:
    def __init__(self, calls):
        self.calls = calls

    def get_company_news(self, ticker, start, end, *, cutoff_at=None):
        self.calls.append(("massive_news", ticker, start, end, cutoff_at))
        return ProviderResponse(
            provenance=_provenance("massive-news"),
            data=CompanyNewsMetadata(ticker=ticker, articles=()),
        )


class _Sec:
    def __init__(self, calls):
        self.calls = calls

    def get_earnings_filings(self, ticker, start, end):
        self.calls.append(("sec", ticker, start, end))
        return ProviderResponse(
            provenance=_provenance("sec_edgar"),
            data=FilingEvidence(ticker=ticker, filings=()),
        )


class _YFinance:
    def __init__(self, calls):
        self.calls = calls

    def get_daily_history(self, ticker, start, end):
        self.calls.append(("yfinance", ticker, start, end))
        return _market(ticker)


def _registry(calls):
    return FreeFirstProviderRegistry(
        yfinance=_YFinance(calls),
        sec=_Sec(calls),
        alpha_vantage=_Alpha(calls),
        finnhub=_Finnhub(calls),
        massive=_Massive(calls),
        fixture=None,
    )


def _retained():
    contract = _contract()
    return contract, collect_live_evidence(
        contract,
        _registry([]),
        _timing(contract),
        clock=lambda: RETRIEVED_AT,
    )


def test_secret_free_run_contract_fails_closed_until_owner_authorizes() -> None:
    contract = load_live_evidence_contract(MANIFEST, RUN_CONFIG)
    assert contract.live_provider_run_authorized is False
    assert contract.ontology_id == "universal"
    calls = []
    with pytest.raises(PermissionError, match="not owner-authorized"):
        collect_live_evidence(
            contract, _registry(calls), _timing(contract), clock=lambda: RETRIEVED_AT
        )
    assert calls == []


def test_collection_reserves_budget_and_is_deterministic() -> None:
    contract = _contract()
    calls = []
    first = collect_live_evidence(
        contract, _registry(calls), _timing(contract), clock=lambda: RETRIEVED_AT
    )
    second = collect_live_evidence(
        contract, _registry([]), _timing(contract), clock=lambda: RETRIEVED_AT
    )
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.budget.total_reserved == 9
    assert first.budget.keyed_reserved == 3
    assert {item.source for item in first.budget.uses} >= {"synthetic_investor_relations"}
    assert first.failures == ()
    assert len(calls) == 6
    assert first.budget.uses[1].source == "alpha_vantage_news"
    assert all(
        response.provenance.origin is not ProviderOrigin.FIXTURE
        for response in (
            first.event,
            first.expectations,
            first.filings,
            first.news,
            first.security_market,
            first.benchmark_market,
        )
        if response is not None
    )


def test_collection_uses_manifest_selected_massive_news_source() -> None:
    contract = _contract().model_copy(update={"planned_news_source": "massive_news"})
    calls = []
    retained = collect_live_evidence(
        contract, _registry(calls), _timing(contract), clock=lambda: RETRIEVED_AT
    )

    assert any(item[0] == "massive_news" for item in calls)
    assert not any(item[0] == "alpha_news" for item in calls)
    assert {item.source for item in retained.budget.uses} >= {"massive_news"}


def test_retained_receipts_rebuild_identical_bundle_without_provider_calls(
    tmp_path: Path,
) -> None:
    contract, retained = _retained()
    store = LiveEvidenceStore()
    path = tmp_path / "synthetic-live-receipts.json"
    first_receipt_digest = store.write(retained, path)
    second_receipt_digest = store.write(retained, path)
    assert first_receipt_digest == second_receipt_digest
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "synthetic-live-receipts.json",
        "synthetic-live-receipts.json.sha256",
    ]
    reloaded = store.read(path)

    first = assemble_live_research_evidence(
        contract,
        retained,
        build_live_news_evidence(contract, retained),
        assembled_at=ASSEMBLED_AT,
    )
    second = assemble_live_research_evidence(
        contract,
        reloaded,
        build_live_news_evidence(contract, reloaded),
        assembled_at=ASSEMBLED_AT,
    )
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.target.ticker == "DEMO"
    assert first.consensus is not None
    assert first.technical is not None
    assert first.prior_cards == ()
    assert {item.component for item in first.missingness} == {
        "historical_analytics",
        "management",
        "news",
        "prior_cards",
    }
    bundle_path = tmp_path / "research-evidence.json"
    ResearchEvidenceStore().write(first, bundle_path)
    assert ResearchEvidenceStore().read(bundle_path).canonical_bytes() == (
        first.canonical_bytes()
    )


def test_time_gates_prevent_future_dating_or_post_release_assembly() -> None:
    contract, retained = _retained()
    with pytest.raises(ValueError, match="on the frozen collection date"):
        contract.require_collection_authorization(RETRIEVED_AT - timedelta(days=1))
    with pytest.raises(ValueError, match="cannot precede"):
        assemble_live_research_evidence(
            contract,
            retained,
            build_live_news_evidence(contract, retained),
            assembled_at=ASSEMBLED_AT - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="must precede the target result"):
        assemble_live_research_evidence(
            contract,
            retained,
            build_live_news_evidence(contract, retained),
            assembled_at=contract.expected_release_at,
        )
    with pytest.raises(ValueError, match="finish strictly before cutoff"):
        contract.require_collection_authorization(ASSEMBLED_AT)


def test_retrospective_replay_is_explicit_deterministic_and_not_backdated() -> None:
    contract, retained = _retained()
    news = build_live_news_evidence(contract, retained)
    replayed_at = contract.expected_release_at + timedelta(days=2)

    first = assemble_live_research_evidence(
        contract,
        retained,
        news,
        assembled_at=replayed_at,
        assembly_mode="retrospective_replay",
    )
    second = assemble_live_research_evidence(
        contract,
        retained,
        news,
        assembled_at=replayed_at,
        assembly_mode="retrospective_replay",
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.bundle_id.startswith("replay:")
    assert first.assembled_at == replayed_at
    assert first.company_context.context.ontology_use_mode.value == (
        "current_method_retrospective"
    )
    assert "retrospective_replay_after_target_event" in (
        first.company_context.receipt.warnings
    )
    with pytest.raises(ValueError, match="cannot precede the target result"):
        assemble_live_research_evidence(
            contract,
            retained,
            news,
            assembled_at=ASSEMBLED_AT,
            assembly_mode="retrospective_replay",
        )


def test_target_event_uses_manifest_before_or_after_market_timing() -> None:
    before_market_contract = _contract().model_copy(
        update={"event_timing": EventTiming.BEFORE_MARKET_OPEN}
    )
    response = _events()
    before_market_event = response.data.events[0].model_copy(
        update={"timing": EventTiming.BEFORE_MARKET_OPEN}
    )
    response = response.model_copy(
        update={
            "data": response.data.model_copy(update={"events": (before_market_event,)})
        }
    )

    assert select_target_event(before_market_contract, response) == before_market_event
    with pytest.raises(ValueError, match="frozen event timing"):
        select_target_event(_contract(), response)


def test_fixture_origin_target_outcome_and_future_market_data_fail_closed() -> None:
    _, retained = _retained()
    payload = retained.model_dump(mode="json")
    payload["event"]["provenance"]["origin"] = "fixture"
    payload["collection_sha256"] = None
    with pytest.raises(ValueError, match="fixture-origin"):
        RetainedLiveEvidence.model_validate(payload)

    payload = retained.model_dump(mode="json")
    payload["event"]["data"]["events"][0]["reported_eps"] = "1.50"
    payload["collection_sha256"] = None
    with pytest.raises(ValueError, match="target outcome"):
        RetainedLiveEvidence.model_validate(payload)

    payload = retained.model_dump(mode="json")
    payload["expectations"]["data"]["events"][0]["reported_eps"] = "1.50"
    payload["collection_sha256"] = None
    with pytest.raises(ValueError, match="target outcome"):
        RetainedLiveEvidence.model_validate(payload)

    payload = retained.model_dump(mode="json")
    payload["security_market"]["data"]["bars"][-1]["trading_date"] = "2026-11-17"
    payload["collection_sha256"] = None
    with pytest.raises(ValueError, match="extends into the target event date"):
        RetainedLiveEvidence.model_validate(payload)


def test_runtime_timing_receipt_must_use_manifest_sources_on_collection_day() -> None:
    contract = _contract()
    timing = _timing(contract)
    wrong_sources = timing.model_copy(
        update={
            "sources": (
                IssuerTimingSource(
                    url="https://example.com/a", response_sha256="a" * 64
                ),
                IssuerTimingSource(
                    url="https://example.com/b", response_sha256="b" * 64
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="sources do not match"):
        collect_live_evidence(
            contract, _registry([]), wrong_sources, clock=lambda: RETRIEVED_AT
        )

    stale = timing.model_copy(update={"checked_at": RETRIEVED_AT - timedelta(days=1)})
    with pytest.raises(ValueError, match="on the collection date"):
        collect_live_evidence(
            contract, _registry([]), stale, clock=lambda: RETRIEVED_AT
        )


def test_private_news_manifest_degrades_without_grant_and_public_projection_is_text_free(
    tmp_path: Path,
) -> None:
    contract, retained = _retained()
    article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="massive-news-private-1",
        headline="Restricted headline about Example Systems",
        source_domain="publisher.example",
        url="https://publisher.example/private-story",
        published_at=RETRIEVED_AT - timedelta(hours=2),
        retrieved_at=RETRIEVED_AT,
        related_tickers=("DEMO",),
        ticker_match="exact",
        ticker_sentiment_label="positive",
    )
    payload = retained.model_dump(mode="json")
    payload["news"]["data"]["articles"] = [article.model_dump(mode="json")]
    payload["collection_sha256"] = None
    retained = RetainedLiveEvidence.model_validate(payload)

    manifest = build_live_news_evidence(contract, retained)
    assert len(manifest.items) == 1
    assert manifest.items[0].content.status.value == "headline_only"
    assert manifest.items[0].content.reason.value == "access_not_permitted"
    assert manifest.items[0].passages.selected_passages == ()
    public = manifest.public_projection()
    serialized = public.canonical_bytes().decode()
    assert article.headline not in serialized
    assert article.url not in serialized
    assert article.provider_article_id not in serialized
    assert "private-story" not in serialized

    store = LiveNewsEvidenceStore()
    private_path = tmp_path / "private.json"
    public_path = tmp_path / "public.json"
    assert store.write_private(manifest, private_path) == store.write_private(
        manifest, private_path
    )
    store.write_public(public, public_path)
    assert store.read_private(private_path) == manifest
    assert store.read_public(public_path) == public


def test_private_provider_summary_is_resolvable_and_public_projection_is_text_free() -> None:
    summary = (
        "Article description: Example Systems raised its revenue outlook to $10 billion.\n\n"
        "Ticker sentiment reasoning: The guidance was above consensus."
    )
    article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="massive-news-summary-1",
        headline="Example Systems raises outlook",
        provider_summary=summary,
        summary_status=NewsSummaryStatus.RETAINED_PERMITTED,
        source_domain="publisher.example",
        url="https://publisher.example/summary-story",
        published_at=RETRIEVED_AT - timedelta(hours=3),
        retrieved_at=RETRIEVED_AT,
        related_tickers=("DEMO",),
        ticker_match="exact",
        ticker_sentiment_label="positive",
        provider_fields={
            "summary_grant_id": "owner-private-research",
            "summary_grant_version": "v1",
            "summary_authorized_use": "private_internal_research",
        },
    )
    manifest = build_news_evidence_manifest(
        manifest_id="summary-private-v1",
        acceptance_manifest_sha256="a" * 64,
        ticker="DEMO",
        cutoff_at=ASSEMBLED_AT,
        collected_at=RETRIEVED_AT,
        provider_response_sha256="b" * 64,
        articles=(article,),
        issuer_terms=("DEMO", "Example Systems"),
        maximum_direct_fetches=0,
    )
    references = select_provider_summary_references(
        manifest,
        event_at=RETRIEVED_AT - timedelta(hours=1),
        issuer_terms=("DEMO", "Example Systems"),
    )

    assert manifest.items[0].content.status.value == "summary_only"
    assert manifest.items[0].content.reason.value == "provider_summary_only"
    assert manifest.items[0].content.rights_status.value == "metadata_only"
    assert manifest.items[0].content.retention_policy.value == "metadata_only"
    assert len(references) == 1
    assert manifest.resolve_provider_summary(references[0]) == summary
    serialized = manifest.public_projection().canonical_bytes().decode()
    assert summary not in serialized
    assert article.headline not in serialized
    assert article.url not in serialized
    assert article.provider_article_id not in serialized


class _SyntheticContentFetcher:
    def __init__(self, body: str) -> None:
        self.body = body

    def fetch(self, article, **kwargs):
        return RightsAwareContentFetcher(
            clock=lambda: RETRIEVED_AT,
            resolver=lambda _: ("93.184.216.34",),
        ).fetch(
            article,
            **kwargs,
            request=lambda url: ContentHttpResponse(
                status_code=200,
                headers={"content-type": "text/html"},
                body=self.body.encode(),
                retrieved_at=RETRIEVED_AT,
                final_url=url,
            ),
        )


def test_private_news_manifest_requires_cited_issuer_relationship_for_passage() -> None:
    contract, retained = _retained()
    article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="massive-news-related-1",
        headline="General AI infrastructure demand rises",
        source_domain="publisher.example",
        url="https://publisher.example/ai-story",
        published_at=RETRIEVED_AT - timedelta(hours=2),
        retrieved_at=RETRIEVED_AT,
        related_tickers=("GOOG", "DEMO"),
        ticker_match="related",
    )
    payload = retained.model_dump(mode="json")
    payload["news"]["data"]["articles"] = [article.model_dump(mode="json")]
    payload["collection_sha256"] = None
    retained = RetainedLiveEvidence.model_validate(payload)
    policy = ContentFetchPolicy(
        source_kind=ContentSourceKind.THIRD_PARTY,
        grant_id="synthetic-excerpt-grant",
        grant_version="v1",
        source_ref="synthetic-test",
        allowed_hosts=("publisher.example",),
        access_permitted=True,
        robots_permitted=True,
        retention_policy=ContentRetentionPolicy.BOUNDED_EXCERPTS,
        max_excerpt_count=2,
        max_excerpt_characters=800,
    )
    generic = build_live_news_evidence(
        contract,
        retained,
        policies_by_host={"publisher.example": policy},
        fetcher=_SyntheticContentFetcher(
            "<article><p>AI infrastructure spending is accelerating across the sector.</p></article>"
        ),
    )
    assert generic.items[0].passages.selected_passages == ()

    issuer_specific = build_live_news_evidence(
        contract,
        retained,
        policies_by_host={"publisher.example": policy},
        fetcher=_SyntheticContentFetcher(
            "<article><p>Example Systems expects AI infrastructure revenue to rise 20 percent.</p></article>"
        ),
    )
    selected = issuer_specific.items[0].passages.selected_passages
    assert len(selected) == 1
    assert (
        issuer_specific.resolve_passage(
            article.provider_article_id, selected[0].paragraph_id
        )
        == selected[0]
    )

    forged = issuer_specific.model_dump(mode="json")
    forged["items"][0]["passages"]["selected_passages"][0]["excerpt"] += " forged"
    forged["manifest_sha256"] = None
    with pytest.raises(ValueError, match="excerpt"):
        LiveNewsEvidenceManifest.model_validate(forged)
