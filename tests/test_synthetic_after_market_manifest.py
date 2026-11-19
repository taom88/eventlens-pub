from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import exchange_calendars as xc
import httpx
import pandas as pd
import pytest

from eventlens.ontologies import OntologyCatalog
from eventlens.product.live_evidence import load_live_evidence_contract
from eventlens.product.live_evidence_collection import revalidate_issuer_timing

MANIFEST_PATH = Path("docs/acceptance/synthetic-after-market-rehearsal.json")
RUN_CONFIG_PATH = Path("tests/fixtures/live_run_configs/synthetic-after-market-rehearsal.json")


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_synthetic_after_market_acceptance_identity_cutoff_and_sources_are_frozen() -> None:
    manifest = _manifest()
    contract = load_live_evidence_contract(MANIFEST_PATH, RUN_CONFIG_PATH)

    assert contract.ticker == "DEMO"
    assert contract.ontology_id == "universal"
    assert (
        OntologyCatalog.packaged()
        .resolve(contract.ontology_id, as_of=contract.information_cutoff_at)
        .ontology_id
        == "universal"
    )
    assert contract.event_date == date(2026, 11, 9)
    assert contract.information_cutoff_at == datetime(2026, 11, 9, 20, 30, tzinfo=UTC)
    assert contract.expected_release_at == datetime(2026, 11, 9, 21, 0, tzinfo=UTC)
    assert contract.conference_call_at == datetime(2026, 11, 9, 22, 0, tzinfo=UTC)
    assert contract.live_provider_run_authorized is False
    assert contract.planned_news_source == "massive_news"
    assert contract.issuer_timing_source == "synthetic_investor_relations"
    assert contract.issuer_timing_urls == tuple(
        sorted(record["url"] for record in manifest["source_confirmation"]["records"])
    )

    authorized = contract.model_copy(update={"live_provider_run_authorized": True})
    with pytest.raises(ValueError, match="on the frozen collection date"):
        authorized.require_collection_authorization(
            datetime(2026, 11, 8, 20, 30, tzinfo=UTC)
        )


def test_synthetic_after_market_acceptance_has_three_prior_events_and_valid_market_sessions() -> None:
    manifest = _manifest()
    priors = manifest["prior_events"]
    assert [item["event_date"] for item in priors] == [
        "2026-08-10",
        "2026-05-11",
        "2026-02-23",
    ]
    assert all(item["card_status"] == "feasible_not_built" for item in priors)
    assert all(
        item["issuer_results_url"].startswith("https://example.com/")
        for item in priors
    )

    calendar = xc.get_calendar(manifest["market"]["exchange_calendar"])
    sessions = calendar.sessions_in_range(
        pd.Timestamp("2026-11-10"), pd.Timestamp("2026-11-16")
    )
    assert [str(session.date()) for session in sessions] == manifest["market"][
        "post_event_trading_dates"
    ]


def test_synthetic_after_market_acceptance_budget_rights_and_checksum_are_secret_safe() -> None:
    manifest = _manifest()
    budget = manifest["call_budget"]
    assert sum(item["count"] for item in budget["planned_requests"]) == 12
    assert (
        sum(
            item["count"] for item in budget["planned_requests"] if item["credentialed"]
        )
        == 6
    )
    assert "massive_news" in {item["source"] for item in budget["planned_requests"]}
    assert manifest["eligibility"]["rights"] == {
        "checked_in_content": "redistributable metadata and source links only",
        "licensed_or_provider_body_in_git": False,
        "public_summary_rule": "Paraphrase supported facts; do not publish provider response bodies or transcript bodies.",
        "rights_exception_required": False,
        "status": "eligible",
    }
    serialized = json.dumps(manifest, sort_keys=True).casefold()
    assert not any(
        forbidden in serialized
        for forbidden in ("api_key", "access_token", "private_path", "/users/")
    )
    expected = MANIFEST_PATH.with_suffix(".json.sha256").read_text().strip()
    assert expected == hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()


def test_synthetic_after_market_issuer_timing_revalidation_hashes_bodies_without_retaining_them() -> None:
    contract = load_live_evidence_contract(
        MANIFEST_PATH, RUN_CONFIG_PATH
    ).model_copy(update={"live_provider_run_authorized": True})
    checked_at = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)
    body = (
        b"<html>Example Systems will report on November 9, 2026 after market "
        b"close, followed by a conference call at 5:00 p.m. ET.</html>"
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, request=request)
        )
    )

    receipt = revalidate_issuer_timing(
        contract,
        client=client,
        clock=lambda: checked_at,
    )

    assert receipt.checked_at == checked_at
    assert tuple(item.url for item in receipt.sources) == contract.issuer_timing_urls
    assert {item.response_sha256 for item in receipt.sources} == {
        hashlib.sha256(body).hexdigest()
    }
    assert {item.retrieval_channel for item in receipt.sources} == {"direct_http"}
    assert {item.hash_scope for item in receipt.sources} == {"raw_response"}
    assert "Example Systems" not in receipt.model_dump_json()
