#!/usr/bin/env python3
"""Collect or assemble one manifest-bound live evidence run."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from eventlens.config import EventLensSettings
from eventlens.product.live_evidence import (
    IssuerTimingRevalidation,
    load_live_evidence_contract,
)
from eventlens.product.live_evidence_assembly import assemble_live_research_evidence
from eventlens.product.live_evidence_collection import (
    collect_live_evidence,
    revalidate_issuer_timing,
)
from eventlens.product.live_evidence_store import (
    LiveEvidenceStore,
    LiveNewsEvidenceStore,
)
from eventlens.product.live_news_evidence import build_live_news_evidence
from eventlens.product.research_evidence import ResearchEvidenceStore
from eventlens.providers.registry import build_free_first_registry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase", choices=("collect", "index-news", "assemble", "replay")
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--issuer-revalidation",
        type=Path,
        help="prebuilt exact-source receipt for a transparently degraded transport",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    contract = load_live_evidence_contract(args.manifest, args.run_config)
    receipt_path = contract.output_root / "live-evidence-receipts.json"
    private_news_path = contract.output_root / "private-news-evidence.json"
    public_news_path = contract.output_root / "public-news-evidence.json"
    now = datetime.now(UTC)

    if args.phase == "collect":
        settings = EventLensSettings.load(args.env_file)
        timing = (
            IssuerTimingRevalidation.model_validate_json(
                args.issuer_revalidation.read_bytes()
            )
            if args.issuer_revalidation is not None
            else revalidate_issuer_timing(contract)
        )
        evidence = collect_live_evidence(
            contract,
            build_free_first_registry(settings),
            timing,
        )
        file_sha256 = LiveEvidenceStore().write(evidence, receipt_path)
        news_manifest = build_live_news_evidence(contract, evidence)
        news_store = LiveNewsEvidenceStore()
        private_news_file_sha256 = news_store.write_private(
            news_manifest, private_news_path
        )
        public_news = news_manifest.public_projection()
        public_news_file_sha256 = news_store.write_public(public_news, public_news_path)
        document = {
            "status": "collected",
            "manifest_id": evidence.manifest_id,
            "collection_sha256": evidence.collection_sha256,
            "file_sha256": file_sha256,
            "receipt_path": str(receipt_path),
            "private_news_manifest_sha256": news_manifest.manifest_sha256,
            "private_news_file_sha256": private_news_file_sha256,
            "public_news_projection_sha256": public_news.projection_sha256,
            "public_news_file_sha256": public_news_file_sha256,
            "collected_at": evidence.collected_at.isoformat(),
            "issuer_revalidated_at": evidence.issuer_timing.checked_at.isoformat(),
            "provider_origins": {
                name: response.provenance.origin.value if response else "missing"
                for name, response in (
                    ("event", evidence.event),
                    ("expectations", evidence.expectations),
                    ("filings", evidence.filings),
                    ("news", evidence.news),
                    ("security_market", evidence.security_market),
                    ("benchmark_market", evidence.benchmark_market),
                )
            },
            "failures": [item.model_dump(mode="json") for item in evidence.failures],
            "assembly_not_before": contract.information_cutoff_at.isoformat(),
            "assembly_before": contract.expected_release_at.isoformat(),
        }
    elif args.phase == "index-news":
        retained = LiveEvidenceStore().read(receipt_path)
        news_manifest = build_live_news_evidence(contract, retained)
        news_store = LiveNewsEvidenceStore()
        private_file_sha = news_store.write_private(news_manifest, private_news_path)
        public_news = news_manifest.public_projection()
        public_file_sha = news_store.write_public(public_news, public_news_path)
        document = {
            "status": "news_indexed",
            "manifest_id": contract.manifest_id,
            "private_news_manifest_sha256": news_manifest.manifest_sha256,
            "private_news_file_sha256": private_file_sha,
            "private_news_path": str(private_news_path),
            "public_news_projection_sha256": public_news.projection_sha256,
            "public_news_file_sha256": public_file_sha,
            "public_news_path": str(public_news_path),
            "item_count": len(news_manifest.items),
            "direct_fetch_count": news_manifest.direct_fetch_count,
        }
    else:
        replay = args.phase == "replay"
        bundle_path = contract.output_root / (
            "research-evidence-replay.json" if replay else "research-evidence.json"
        )
        assembled_at = now
        if replay and bundle_path.exists():
            prior_replay = ResearchEvidenceStore().read(bundle_path)
            if (
                not prior_replay.bundle_id.startswith("replay:")
                or prior_replay.target.event_id != contract.manifest_id
            ):
                raise ValueError("existing replay artifact does not match the manifest")
            assembled_at = prior_replay.assembled_at
        retained = LiveEvidenceStore().read(receipt_path)
        news_manifest = LiveNewsEvidenceStore().read_private(private_news_path)
        bundle = assemble_live_research_evidence(
            contract,
            retained,
            news_manifest,
            assembled_at=assembled_at,
            assembly_mode="retrospective_replay" if replay else "live",
        )
        file_sha256 = ResearchEvidenceStore().write(bundle, bundle_path)
        document = {
            "status": "replayed" if replay else "assembled",
            "assembly_mode": "retrospective_replay" if replay else "live",
            "manifest_id": contract.manifest_id,
            "bundle_id": bundle.bundle_id,
            "bundle_sha256": bundle.bundle_sha256,
            "file_sha256": file_sha256,
            "bundle_path": str(bundle_path),
            "assembled_at": bundle.assembled_at.isoformat(),
            "missingness": [
                item.model_dump(mode="json") for item in bundle.missingness
            ],
        }
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
