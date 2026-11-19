"""Regenerate the checked-in R1 external-agent fixture corpus."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eventlens.agents import (
    CanonicalMetric,
    DeterministicExternalAgentFixture,
    ExternalEvidence,
    assemble_external_bundle,
)


def build_package() -> dict[str, object]:
    now = datetime(2026, 10, 24, 16, tzinfo=timezone.utc)
    bundle = assemble_external_bundle(
        bundle_id="fixture-demo-2026q3-r1",
        session_id="fixture-external-agent-v1",
        event_id="DEMO:2026-10-29:2026Q3",
        ticker="DEMO",
        route="external",
        bundle_revision=1,
        built_at=now,
        information_cutoff_at=now,
        target_release_at=datetime(2026, 10, 29, 20, tzinfo=timezone.utc),
        target_release_status="assumed_after_market_close",
        research_question="Prepare Example Systems earnings research.",
        canonical_metrics=(
            CanonicalMetric(
                metric_id="consensus_eps",
                value=Decimal("2.001"),
                unit="USD_PER_SHARE",
                period="2026Q3",
                as_of=now,
                calculation_version="provider-normalization-v1",
                provenance_ids=("finnhub-calendar",),
            ),
        ),
        evidence=(
            ExternalEvidence(
                source_id="eventlens-context-card:demo-context-2026-07-30",
                passage_id="executive-summary",
                evidence_kind="historical_context",
                typed_summary="Prior event evidence highlights cloud demand and capital intensity.",
                available_at=now,
                content_sha256="a" * 64,
                rights_status="summary_permitted",
                retention_status="summary_only",
                provenance_ref="demo-context-2026-07-30",
            ),
        ),
        missingness=("provider-native news sentiment unavailable",),
        unavailable_stages=("company-specific valuation",),
        forbidden_behaviors=("change canonical metrics", "use post-cutoff evidence"),
    )
    report = DeterministicExternalAgentFixture().interpret(bundle)
    valid = report.model_dump(mode="json")
    invalid: dict[str, dict[str, object]] = {}

    def case(name: str, code: str, mutate) -> None:
        payload = copy.deepcopy(valid)
        mutate(payload)
        invalid[name] = {"expected_code": code, "report": payload}

    case("stale_checksum", "binding_mismatch", lambda p: p.__setitem__("bundle_sha256", "b" * 64))
    case(
        "unknown_citation",
        "unknown_citation",
        lambda p: p["claims"][0]["citations"][0].__setitem__("passage_id", "missing"),
    )
    case(
        "unknown_metric",
        "unknown_metric",
        lambda p: p["scenarios"][0].__setitem__("canonical_metric_ids", ["invented_metric"]),
    )
    case("bad_probability", "schema_invalid", lambda p: p["scenarios"][0].__setitem__("probability", "0.90"))
    case("bad_action", "schema_invalid", lambda p: p["recommendation"].__setitem__("action", "TRADE_NOW"))
    case(
        "unsupported_zone",
        "schema_invalid",
        lambda p: p.__setitem__(
            "valuation_zones",
            [{
                "zone_id": "unsupported",
                "method": "unknown",
                "basis": "none",
                "currency": "USD",
                "lower": "100",
                "upper": "200",
                "assumption_refs": [],
                "calculation_refs": [],
                "metric_refs": [],
                "citations": [],
            }],
        ),
    )
    case("duplicate_claim_id", "schema_invalid", lambda p: p.__setitem__("claims", p["claims"] * 2))
    return {
        "schema_version": "1.0",
        "bundle": bundle.model_dump(mode="json"),
        "valid_report": valid,
        "invalid_reports": invalid,
    }


def main() -> None:
    root = Path(__file__).parents[1]
    destination = root / "fixtures" / "external_agent" / "v1" / "cases.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(build_package(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
