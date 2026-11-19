from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx

from eventlens.ontologies import OntologyCatalog
from eventlens.product import (
    CitedCompanyStatement,
    CompanyContextSource,
    CompanyContextStore,
    CompanySlotObservation,
    ContextSourceType,
    OntologyUseMode,
    build_company_context,
)
from eventlens.providers import FinnhubProvider, NewsTickerMatch
from eventlens.providers.http import CachedHttpClient


def test_ontology_context_and_news_discovery_integrate_without_event_leakage(tmp_path) -> None:
    cutoff = datetime(2026, 10, 24, 20, tzinfo=timezone.utc)
    published = int((cutoff - timedelta(hours=2)).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": 7, "datetime": published, "headline": "Example Systems adds capacity",
            "summary": "The provider reports additional cloud infrastructure capacity.",
            "source": "Example Wire", "category": "company", "related": "DEMO",
            "url": "https://example.com/cloud-capacity",
        }])

    http = CachedHttpClient(
        tmp_path / "cache", client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: cutoff,
    )
    news = FinnhubProvider("secret", http, allow_provider_summary_retention=True).get_company_news(
        "DEMO", date(2026, 10, 1), cutoff.date(), cutoff_at=cutoff,
    )
    article = news.data.articles[0]
    assert article.ticker_match is NewsTickerMatch.EXACT
    assert article.provider_summary and article.discovery_fingerprint

    source = CompanyContextSource(
        source_id="demo-10k", source_type=ContextSourceType.ISSUER_FILING,
        source_sha256="b" * 64, available_at=cutoff - timedelta(days=60),
        citation="Example Systems Form 10-K", source_url="https://example.com/demo-10k",
        citation_locator="Item 1",
    )
    statement = CitedCompanyStatement(text="Example Systems operates cloud and commerce segments.", source_ids=(source.source_id,))
    artifact = build_company_context(
        ticker="DEMO", artifact_version=1, built_at=cutoff,
        information_cutoff_at=cutoff,
        ontology=OntologyCatalog.packaged().resolve("sector.internet_platform"),
        sources=(source,), observations=(CompanySlotObservation(
            slot_id="identity.ticker", value="DEMO", unit="text",
            fact_period="current",
            as_of_at=source.available_at, source_ids=(source.source_id,),
        ),), identity=(statement,), management=(), segments=(statement,),
        revenue_drivers=(statement,), historical_overview=(statement,),
        target_event_id="DEMO:2026-10-29:2026Q3",
        ontology_use_mode=OntologyUseMode.CURRENT_METHOD_RETROSPECTIVE,
    )
    target = tmp_path / "artifacts" / "demo-company-context.json"
    CompanyContextStore().write(artifact, target)
    reloaded = CompanyContextStore().read(target)
    assert reloaded.ontology_sha256 == OntologyCatalog.packaged().resolve("sector.internet_platform").content_sha256
    assert not hasattr(reloaded, "event_id")
    assert "secret" not in target.read_text()
