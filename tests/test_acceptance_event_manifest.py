from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "acceptance" / "synthetic-after-market-event.json"
CHECKSUM_PATH = MANIFEST_PATH.with_suffix(".json.sha256")


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_acceptance_event_identity_cutoff_and_calendar_are_frozen() -> None:
    manifest = _manifest()
    event = manifest["event"]
    cutoff = datetime.fromisoformat(manifest["information_barrier"]["cutoff_at"])
    release = datetime.fromisoformat(event["expected_results_release_at"])
    call = datetime.fromisoformat(event["conference_call_at"])

    assert manifest["schema_version"] == "eventlens.acceptance_event.v1"
    assert manifest["selection"] == {
        "owner_selected": True,
        "status": "frozen",
        "ticket_id": "EL-003",
    }
    assert manifest["issuer"]["ticker"] == "DEMO"
    assert event["fiscal_period"] == "FY2027Q3"
    assert date.fromisoformat(manifest["frozen_on"]) < date.fromisoformat(
        event["event_date"]
    )
    assert cutoff < release < call
    assert manifest["information_barrier"]["target_outcome_before_lock"] == "forbidden"

    import exchange_calendars as xc
    import pandas as pd

    calendar = xc.get_calendar(manifest["market"]["exchange_calendar"])
    sessions = calendar.sessions_in_range(
        pd.Timestamp("2026-11-18"), pd.Timestamp("2026-11-24")
    )
    assert [str(session.date()) for session in sessions] == manifest["market"][
        "post_event_trading_dates"
    ]


def test_acceptance_event_has_three_feasible_prior_cards_and_source_paths() -> None:
    manifest = _manifest()
    priors = manifest["prior_events"]

    assert manifest["eligibility"]["history"]["status"] == "feasible_not_built"
    assert len(priors) == manifest["eligibility"]["history"]["required_card_count"] == 3
    assert len({item["fiscal_period"] for item in priors}) == 3
    assert all(item["card_status"] == "feasible_not_built" for item in priors)
    assert all(
        item["issuer_results_url"].startswith("https://example.com/")
        for item in priors
    )
    assert all(len(item["planned_evidence"]) == 5 for item in priors)
    assert all(
        date.fromisoformat(item["event_date"])
        < date.fromisoformat(manifest["event"]["event_date"])
        for item in priors
    )

    confirmations = manifest["source_confirmation"]
    assert confirmations["runtime_revalidation_required"] is True
    assert {item["confirmation_class"] for item in confirmations["records"]} >= {
        "issuer_primary",
        "issuer_event_record",
        "secondary_schedule_corroboration",
    }
    assert manifest["eligibility"]["consensus"]["status"] == "provider_path_available"
    assert manifest["eligibility"]["issuer_primary_material"]["status"] == "available"


def test_acceptance_event_budget_rights_and_checksum_are_secret_safe() -> None:
    manifest = _manifest()
    budget = manifest["call_budget"]
    rights = manifest["eligibility"]["rights"]
    serialized = json.dumps(manifest, sort_keys=True).casefold()

    assert sum(item["count"] for item in budget["planned_requests"]) == budget[
        "hard_total_request_ceiling"
    ]
    assert sum(
        item["count"] for item in budget["planned_requests"] if item["credentialed"]
    ) == budget["hard_keyed_request_ceiling"]
    assert rights["licensed_or_provider_body_in_git"] is False
    assert rights["rights_exception_required"] is False
    assert not any(
        forbidden in serialized
        for forbidden in ("api_key", "access_token", "private_path", "/users/")
    )

    expected = CHECKSUM_PATH.read_text(encoding="utf-8").strip()
    observed = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    assert expected == observed
