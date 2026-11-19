from __future__ import annotations

import copy
from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eventlens.context_cards import ContextCardV4, SCHEMA_VERSION, context_card_json_schema
from eventlens.context_cards.history import EventTimestampEvidence


ROOT = Path(__file__).resolve().parents[2]
GENERATED_AT = "2026-09-09T03:42:16.107760+00:00"
HISTORICAL_EVENT_CUTOFF = "2026-09-08T20:00:00+00:00"


def _minimal_card() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "ticker": "DEMO",
            "company": "Example Systems Corporation",
            "event": {
                "type": "earnings",
                "fiscal_period": "FY2027_Q1",
                "event_datetime": None,
                "timezone": None,
                "event_datetime_status": "unknown",
            },
            "card": {
                "version": 4,
                "generated_at": GENERATED_AT,
                "as_of": "2026-09-09T00:00:00+00:00",
                "sha256": None,
                "status": "draft",
            },
        },
        "temporal_state": {
            "historical_event_cutoff": HISTORICAL_EVENT_CUTOFF,
            "observations": {},
        },
        "decision_context": {
            "decision_type": "pre_earnings_event_trade",
            "reference_price": {"value": None, "status": "unknown"},
            "entry_threshold": {"value": None, "status": "not_computed"},
            "currency": "USD",
            "long_term_view_explicitly_separate": True,
            "decision_status": "not_evaluated",
        },
        "current_pre_event_state": {},
        "company_ontology": {
            "ontology_id": "demo_kpi_ontology",
            "version": "1.0.0-proposed.1",
            "reference": "package://eventlens.context_cards.ontologies/DEMO.yaml",
        },
        "historical_quarters": [],
        "management_guidance_ledger": [],
        "consensus_state": {},
        "market_reaction_history": [],
        "thesis_state": {},
        "scenario_model": {},
        "decision": {},
        "evidence_registry": {},
        "conflicts": [],
        "quality_control": {},
        "provenance": {
            "schema_version": SCHEMA_VERSION,
            "ontology_version": "1.0.0-proposed.1",
            "generated_at": GENERATED_AT,
        },
    }


def _card_with_proxy_timestamp() -> dict[str, object]:
    payload = copy.deepcopy(_minimal_card())
    payload["evidence_registry"] = {  # type: ignore[index]
        "receipts": [
            {
                "id": "event-timestamp-proxy:fy2026q1",
                "evidence_class": "source_native",
                "claim": (
                    "FY2026Q1 event timestamp is bounded to the SEC EDGAR "
                    "acceptance time of the first verified public artifact only."
                ),
                "source_ref": (
                    "https://www.sec.gov/Archives/edgar/data/789019/"
                    "000119312525256321/demo-20250930.htm#sha256="
                    "13d9bf415530f2feaab64e34602f2d709e7824cf87850b07c66a6138c1887ca5"
                ),
            }
        ]
    }
    payload["historical_quarters"] = [  # type: ignore[index]
        {
            "fiscal_period": {
                "fiscal_year": 2026,
                "fiscal_quarter": 1,
                "period_end": "2025-09-30",
            },
            "event_timestamp_evidence": {
                "timestamp_kind": "first_verified_public_artifact_proxy",
                "first_verified_at": "2025-10-29T20:10:48+00:00",
                "timezone": "UTC",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/789019/"
                    "000119312525256321/demo-20250930.htm"
                ),
                "source_sha256": "13d9bf415530f2feaab64e34602f2d709e7824cf87850b07c66a6138c1887ca5",
                "acceptance_receipt": (
                    "sec-edgar-acceptance:0001193125-25-256321:"
                    "2025-10-29T20:10:48+00:00"
                ),
                "evidence_receipt_id": "event-timestamp-proxy:fy2026q1",
                "policy_version": "eventlens.timestamp_proxy.acceptance_receipt.v1",
            },
            "expectations": [
                {
                    "consensus_type": "sell_side",
                    "fiscal_period": {
                        "fiscal_year": 2026,
                        "fiscal_quarter": 1,
                        "period_end": "2025-09-30",
                    },
                    "metrics": {},
                    "evidence_refs": ["consensus:placeholder"],
                    "availability_status": "unknown",
                }
            ],
            "actual_results": {
                "fiscal_period": {
                    "fiscal_year": 2026,
                    "fiscal_quarter": 1,
                    "period_end": "2025-09-30",
                },
                "metrics": {},
                "evidence_refs": ["actual:placeholder"],
            },
            "surprise_layers": {},
            "reaction_missing_reason": "market reaction not staged",
            "evidence_refs": ["actual:placeholder"],
        }
    ]
    return payload


def test_model_and_checked_in_schema_round_trip() -> None:
    card = ContextCardV4.model_validate(_minimal_card())
    serialized = card.model_dump_json()

    assert ContextCardV4.model_validate_json(serialized) == card
    checked_in = json.loads(
        (ROOT / "schemas" / "eventlens.context_card.v4.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert checked_in == context_card_json_schema()
    assert checked_in["additionalProperties"] is False
    expected_sections = {
        "schema_version",
        "identity",
        "temporal_state",
        "decision_context",
        "current_pre_event_state",
            "company_ontology",
            "tier_1_observations",
        "historical_quarters",
        "management_guidance_ledger",
        "consensus_state",
        "market_reaction_history",
        "thesis_state",
        "scenario_model",
        "decision",
        "evidence_registry",
        "conflicts",
        "quality_control",
        "provenance",
    }
    assert set(checked_in["properties"]) == expected_sections
    assert set(card.model_dump(mode="json")) == expected_sections


def test_missing_fiscal_identity_is_rejected() -> None:
    payload = copy.deepcopy(_minimal_card())
    del payload["identity"]["event"]["fiscal_period"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="fiscal_period"):
        ContextCardV4.model_validate(payload)


def test_compact_ontology_reference_and_matching_provenance_are_required() -> None:
    payload = copy.deepcopy(_minimal_card())
    del payload["company_ontology"]["reference"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="company_ontology.reference"):
        ContextCardV4.model_validate(payload)

    payload = copy.deepcopy(_minimal_card())
    payload["provenance"]["ontology_version"] = "0.9.0"  # type: ignore[index]

    with pytest.raises(ValidationError, match="must match provenance ontology_version"):
        ContextCardV4.model_validate(payload)


def test_zero_cannot_substitute_for_explicitly_unknown_threshold() -> None:
    payload = copy.deepcopy(_minimal_card())
    payload["decision_context"]["entry_threshold"] = {  # type: ignore[index]
        "value": 0,
        "status": "unknown",
    }

    with pytest.raises(ValidationError, match="unknown numeric value must remain null"):
        ContextCardV4.model_validate(payload)


def _revenue_metric() -> dict[str, object]:
    return {
        "metric": "revenue",
        "value": "77.673",
        "unit": "USD_billion",
        "fiscal_period": {"fiscal_year": 2026, "fiscal_quarter": 1},
        "accounting_basis": "gaap",
        "growth_basis": "not_applicable",
        "scope": "consolidated",
    }


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("current_pre_event_state", "latest_actuals"),
        ("current_pre_event_state", "current_management_guidance"),
        ("consensus_state", "metrics"),
    ],
)
def test_raw_scalar_metrics_are_rejected(
    section: str,
    field: str,
) -> None:
    payload = _minimal_card()
    payload[section][field] = {"revenue": "77.673"}  # type: ignore[index]

    with pytest.raises(ValidationError):
        ContextCardV4.model_validate(payload)


def test_typed_metric_value_is_accepted_in_numeric_card_collections() -> None:
    payload = _minimal_card()
    payload["current_pre_event_state"]["latest_actuals"] = {  # type: ignore[index]
        "revenue": _revenue_metric()
    }
    payload["current_pre_event_state"]["current_management_guidance"] = {  # type: ignore[index]
        "revenue": _revenue_metric()
    }
    payload["consensus_state"]["metrics"] = {"revenue": _revenue_metric()}  # type: ignore[index]

    card = ContextCardV4.model_validate(payload)

    assert card.current_pre_event_state.latest_actuals["revenue"].value == Decimal("77.673")
    assert card.consensus_state.metrics["revenue"].fiscal_period.label == "FY2026_Q1"


def test_naive_datetimes_and_extra_fields_are_rejected() -> None:
    payload = copy.deepcopy(_minimal_card())
    payload["identity"]["card"]["generated_at"] = "2026-09-09T03:42:16"  # type: ignore[index]
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        ContextCardV4.model_validate(payload)


def test_temporal_state_rejects_observation_classified_against_another_cutoff() -> None:
    payload = _minimal_card()
    payload["temporal_state"]["observations"] = {  # type: ignore[index]
        "sec_10q_fy2026_q4": {
            "source_published_at": "2026-07-29T20:05:00+00:00",
            "provider_observed_at": "2026-07-29T20:06:00+00:00",
            "first_known_available_at": "2026-07-29T20:05:00+00:00",
            "historical_event_cutoff": "2026-09-07T20:00:00+00:00",
            "available_before_event": {
                "value": "available",
                "evidence": ["issuer_timestamp"],
            },
            "reconstruction": {
                "is_reconstructed": False,
                "reconstructed_at": None,
            },
            "temporal_confidence": "high",
            "fact_truth_status": "verified",
            "classification": "native",
        }
    }

    with pytest.raises(ValidationError, match="must match temporal_state cutoff"):
        ContextCardV4.model_validate(payload)


def test_temporal_state_carries_classified_observations_at_the_declared_cutoff() -> None:
    payload = _minimal_card()
    payload["temporal_state"]["observations"] = {  # type: ignore[index]
        "sec_10q_fy2026_q4": {
            "source_published_at": "2026-07-29T20:05:00+00:00",
            "provider_observed_at": "2026-07-29T20:06:00+00:00",
            "first_known_available_at": "2026-07-29T20:05:00+00:00",
            "historical_event_cutoff": HISTORICAL_EVENT_CUTOFF,
            "available_before_event": {
                "value": "available",
                "evidence": ["issuer_timestamp"],
            },
            "reconstruction": {
                "is_reconstructed": False,
                "reconstructed_at": None,
            },
            "temporal_confidence": "high",
            "fact_truth_status": "verified",
            "classification": "native",
        }
    }

    card = ContextCardV4.model_validate(payload)

    observation = card.temporal_state.observations["sec_10q_fy2026_q4"]
    assert observation.historical_event_cutoff == card.temporal_state.historical_event_cutoff
    assert observation.availability_value.value == "available"


def test_proxy_timestamp_evidence_accepts_exact_bounded_receipt_backed_utc() -> None:
    card = ContextCardV4.model_validate(_card_with_proxy_timestamp())

    evidence = card.historical_quarters[0].event_timestamp_evidence
    assert evidence is not None
    assert evidence.timestamp_kind == "first_verified_public_artifact_proxy"
    assert "SEC acceptance timestamp proxy only" in evidence.limitation


def test_proxy_timestamp_rejects_date_only_source_less_unlinked_and_later_values() -> None:
    with pytest.raises(ValidationError):
        EventTimestampEvidence.model_validate(
            {
                "first_verified_at": "2025-10-29",
                "source_url": "https://www.sec.gov/Archives/example.htm",
                "source_sha256": "a" * 64,
                "acceptance_receipt": "sec-edgar-acceptance:example",
                "evidence_receipt_id": "event-timestamp-proxy:example",
            }
        )

    for field in ("source_url", "source_sha256", "acceptance_receipt"):
        payload = _card_with_proxy_timestamp()
        del payload["historical_quarters"][0]["event_timestamp_evidence"][field]  # type: ignore[index]
        with pytest.raises(ValidationError):
            ContextCardV4.model_validate(payload)

    payload = _card_with_proxy_timestamp()
    payload["historical_quarters"][0]["event_timestamp_evidence"][  # type: ignore[index]
        "evidence_receipt_id"
    ] = "missing-receipt"
    with pytest.raises(ValidationError, match="link to a registry receipt"):
        ContextCardV4.model_validate(payload)

    payload = _card_with_proxy_timestamp()
    payload["historical_quarters"][0]["event_timestamp_evidence"][  # type: ignore[index]
        "first_verified_at"
    ] = "2026-09-09T00:00:01+00:00"
    with pytest.raises(ValidationError, match="later than card as_of"):
        ContextCardV4.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "unexpected"),
    [
        ("temporal_state", {"available_before_event": True}),
        ("decision", {"action": "buy"}),
        ("evidence_registry", {"raw_provider_body": "not allowed"}),
    ],
)
def test_reserved_sections_are_strict_and_decision_neutral(
    section: str, unexpected: dict[str, object]
) -> None:
    payload = copy.deepcopy(_minimal_card())
    payload[section] = unexpected

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ContextCardV4.model_validate(payload)
