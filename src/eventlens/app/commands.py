"""Small operational CLI for configuration and provider preflight."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from eventlens.artifacts.replay import write_fixture_replay
from eventlens.config import EventLensSettings
from eventlens.learning.readiness import audit_learning_readiness
from eventlens.live_preflight import run_upcoming_preflight
from eventlens.product.ask import write_research_markdown
from eventlens.product.research_evidence import (
    ResearchEvidenceBundle,
    ResearchEvidenceStore,
)
from eventlens.providers.registry import build_free_first_registry
from eventlens.quant.backtest import run_event_study
from eventlens.research_engines import VibeMcpEvidenceClient
from eventlens.web.product_session import ProductSessionFacade, SessionMode


class _ProvidedEvidenceSource:
    """One validated universal input bound to one issuer instance."""

    def __init__(self, bundle: ResearchEvidenceBundle) -> None:
        self.bundle = bundle

    def build(
        self, *, ticker: str, question: str, session_id: str
    ) -> ResearchEvidenceBundle:
        del question, session_id
        if ticker != self.bundle.target.ticker:
            raise ValueError("--ticker must match the evidence-bundle target")
        return self.bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eventlens")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "demo",
        help="run the bundled key-free neutral fixture demo",
    )
    commands.add_parser("app", help="launch the local Streamlit interface")
    commands.add_parser(
        "config-check", help="show key presence without printing secrets"
    )
    commands.add_parser("provider-plan", help="show free-first provider routing")
    preflight = commands.add_parser(
        "upcoming-preflight",
        aliases=["upcoming-demo-preflight"],
        help="resolve one upcoming equity event and write a read-only preview artifact",
    )
    preflight.add_argument("--ticker", required=True)
    preflight.add_argument("--window-start", type=date.fromisoformat, required=True)
    preflight.add_argument("--window-end", type=date.fromisoformat, required=True)
    preflight.add_argument(
        "--output",
        type=Path,
        default=Path(".eventlens-cache/preflight/upcoming-live-preview.json"),
    )
    backtest = commands.add_parser(
        "event-study-backtest",
        help="run a historical ticker-agnostic earnings reaction study; no recommendations are invented",
    )
    backtest.add_argument("--ticker", required=True)
    backtest.add_argument("--start", type=date.fromisoformat, required=True)
    backtest.add_argument("--end", type=date.fromisoformat, required=True)
    backtest.add_argument("--output", type=Path, required=True)
    replay = commands.add_parser(
        "replay",
        help="create and verify one key-free deterministic fixture replay artifact",
    )
    replay.add_argument("--ticker", required=True)
    replay.add_argument("--event", dest="event_id", required=True)
    replay.add_argument("--fixture-root", type=Path, default=Path("fixtures"))
    replay.add_argument("--output", type=Path)
    replay.add_argument("--session-id")
    ask = commands.add_parser(
        "research-ask",
        help="turn a plain-language earnings request into a deterministic research run",
    )
    ask.add_argument("--question", required=True)
    ask.add_argument(
        "--output-dir", type=Path, default=Path(".eventlens-cache/research-asks")
    )
    ask.add_argument("--session-id")
    ask.add_argument(
        "--ticker", help="explicit target symbol; must match the evidence bundle"
    )
    ask.add_argument(
        "--evidence-bundle",
        type=Path,
        required=True,
        help="validated eventlens.research-evidence.v1 JSON; universal synthesis input",
    )
    render_ask = commands.add_parser(
        "research-render",
        help="render an existing research-ask JSON artifact to Markdown without provider calls",
    )
    render_ask.add_argument("--input", type=Path, required=True)
    render_ask.add_argument("--output", type=Path)
    import_agent = commands.add_parser(
        "research-import-agent-report",
        help="validate a report and resume its repository-backed product session",
    )
    import_agent.add_argument("--session-id", required=True)
    import_agent.add_argument("--report", type=Path, required=True)
    import_agent.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".eventlens-cache/research-asks"),
        help="the same product-session root used by research-ask",
    )
    smoke = commands.add_parser(
        "provider-smoke",
        help="make one secret-safe live/cached provider call through an EventLens adapter",
    )
    smoke.add_argument(
        "--provider", choices=("alpha_vantage", "finnhub", "massive"), required=True
    )
    smoke.add_argument(
        "--capability",
        choices=("earnings-events", "transcript", "daily-history", "company-news"),
        required=True,
    )
    smoke.add_argument("--ticker", required=True)
    smoke.add_argument("--start", type=date.fromisoformat)
    smoke.add_argument("--end", type=date.fromisoformat)
    smoke.add_argument("--cutoff-at", type=datetime.fromisoformat)
    smoke.add_argument("--quarter", default="2026Q2")
    commands.add_parser(
        "vibe-check", help="preflight the configured Vibe MCP transport and schemas"
    )
    commands.add_parser(
        "agent-check", help="preflight the direct report/follow-up agent"
    )
    commands.add_parser(
        "learning-audit",
        help="audit whether immutable post-event records support offline learning",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "demo":
        from eventlens.product.fixture_replay import run_d0_demo_fixture_replay

        try:
            run = run_d0_demo_fixture_replay(Path(".eventlens-cache/demo-handoff"))
            document = {
                "status": run.imported.status.value,
                "fixture_id": run.package.manifest.fixture_id,
                "session_id": run.package.manifest.session_id,
                "context_cards": len(run.package.cards),
                "provider_calls": run.package.manifest.provider_calls,
                "model_calls": run.package.manifest.model_calls,
                "notice": "Synthetic demonstration data; not actual company history or investment guidance.",
            }
            print(json.dumps(document, indent=2))
            return 0
        except Exception as error:
            print(
                json.dumps(
                    {"error": type(error).__name__, "detail": str(error)}, indent=2
                )
            )
            return 1
    if args.command == "app":
        import subprocess

        from eventlens.web.streamlit_app import __file__ as app_path

        return subprocess.call([sys.executable, "-m", "streamlit", "run", app_path])
    settings = EventLensSettings.load(args.env_file)
    if args.command == "config-check":
        reasoning_ready, reasoning_reason = settings.vibe_reasoning_readiness()
        agent_ready, agent_reason = settings.agent_reasoning_readiness()
        credentials = settings.credential_statuses()
        no_key_sources = [
            "yfinance",
            "restricted Vibe MCP yfinance route (pre-lock core call)",
            "Vibe Stooq/Yahoo fallbacks (broad CLI post-lock only)",
        ]
        if any(
            item.provider == "SEC EDGAR identity" and item.configured
            for item in credentials
        ):
            no_key_sources.insert(0, "SEC EDGAR")
        document = {
            "credentials": [item.model_dump(mode="json") for item in credentials],
            "no_key_sources": no_key_sources,
            "vibe_reasoning_ready": reasoning_ready,
            "vibe_reasoning_reason": reasoning_reason,
            "vibe_keyed_market_data_enabled": settings.allow_vibe_keyed_market_data,
            "agent_reasoning_ready": agent_ready,
            "agent_reasoning_reason": agent_reason,
            "agent_provider": settings.agent_provider,
            "agent_model": settings.agent_model,
            "data_mode": settings.data_mode,
            "key_values_printed": False,
        }
        exit_code = 0
    elif args.command == "provider-plan":
        document = {
            "providers": [
                item.model_dump(mode="json")
                for item in build_free_first_registry(settings).plan()
            ]
        }
        exit_code = 0
    elif args.command == "upcoming-preflight":
        try:
            document = run_upcoming_preflight(
                settings,
                ticker=args.ticker,
                window_start=args.window_start,
                window_end=args.window_end,
                output=args.output,
            )
            exit_code = 0
        except Exception as error:
            document = {"error": type(error).__name__, "detail": str(error)}
            exit_code = 1
    elif args.command == "event-study-backtest":
        try:
            registry = build_free_first_registry(settings)
            if registry.alpha_vantage is None or registry.yfinance is None:
                raise RuntimeError(
                    "historical backtest requires Alpha Vantage and yfinance"
                )
            events = registry.alpha_vantage.get_earnings_events(
                args.ticker, args.start, args.end
            )
            history = registry.yfinance.get_daily_history(
                args.ticker, args.start, args.end
            )
            report = run_event_study(args.ticker, events.data.events, history.data)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                report.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            document = report.model_dump(mode="json")
            exit_code = 0
        except Exception as error:
            document = {"error": type(error).__name__, "detail": str(error)}
            exit_code = 1
    elif args.command == "replay":
        try:
            output = (
                args.output
                or Path(".eventlens-cache/replays")
                / f"{args.ticker.upper()}-{args.event_id}.json"
            )
            envelope, digest = write_fixture_replay(
                ticker=args.ticker,
                event_id=args.event_id,
                fixture_root=args.fixture_root,
                output=output,
                session_id=args.session_id,
            )
            document = {
                "artifact_type": envelope.artifact_type,
                "artifact_id": envelope.artifact_id,
                "session_id": envelope.session_id,
                "event_id": envelope.event_id,
                "path": str(output),
                "sha256": digest,
                "payload_checksum_valid": envelope.verify_payload_checksum(),
                "fixture_notice": envelope.payload["fixture_notice"],
            }
            exit_code = 0
        except Exception as error:
            document = {"error": type(error).__name__, "detail": str(error)}
            exit_code = 1
    elif args.command == "research-ask":
        try:
            evidence_bundle = ResearchEvidenceStore().read(args.evidence_bundle)
            ticker = (args.ticker or evidence_bundle.target.ticker).strip().upper()
            facade = ProductSessionFacade(
                settings,
                session_root=args.output_dir,
                evidence_source=_ProvidedEvidenceSource(evidence_bundle),
            )
            session = facade.submit(
                args.question,
                SessionMode.LIVE,
                session_id=args.session_id,
                ticker=ticker,
            )
            document = session.model_dump(mode="json")
            document["artifact_files"] = {
                item.kind: {
                    "path": str(item.path),
                    "sha256": item.sha256,
                    "verified": item.verified,
                }
                for item in session.artifact_refs
            }
            exit_code = (
                2
                if session.status == "error" or session.status.startswith("blocked_")
                else 0
            )
        except Exception as error:
            document = {"error": type(error).__name__, "detail": str(error)}
            exit_code = 1
    elif args.command == "research-render":
        try:
            document = json.loads(args.input.read_text(encoding="utf-8"))
            output = args.output or args.input.with_suffix(".md")
            _, checksum, digest = write_research_markdown(document, output)
            document = {
                "source": str(args.input),
                "markdown": str(output),
                "markdown_sha256": str(checksum),
                "digest": digest,
                "provider_calls": 0,
            }
            exit_code = 0
        except Exception as error:
            document = {"error": type(error).__name__, "detail": str(error)}
            exit_code = 1
    elif args.command == "research-import-agent-report":
        try:
            facade = ProductSessionFacade(settings, session_root=args.output_dir)
            imported = facade.import_report(
                args.session_id,
                args.report.read_bytes(),
            )
            if not imported.ok:
                message = (
                    imported.failure.message
                    if imported.failure is not None
                    else "The external-agent report could not be imported."
                )
                raise ValueError(message)
            session = facade.load(args.session_id)
            document = session.model_dump(mode="json")
            assert imported.report is not None
            document["validated_report_id"] = imported.report.report_id
            document["provider_calls"] = 0
            exit_code = 0
        except Exception as error:
            document = {"error": type(error).__name__, "detail": str(error)}
            exit_code = 1
    elif args.command == "provider-smoke":
        try:
            document = _provider_smoke(args, settings)
            exit_code = 0
        except Exception as error:
            document = {"error": type(error).__name__, "detail": str(error)}
            exit_code = 1
    elif args.command == "vibe-check":
        reasoning_ready, reasoning_reason = settings.vibe_reasoning_readiness()
        client = VibeMcpEvidenceClient(
            settings.vibe_mcp_connection_settings(),
            artifact_dir=settings.vibe_artifact_dir / "mcp-evidence",
            enable_postlock_broad_research=settings.enable_postlock_vibe_broad_research,
        )
        preflight = asyncio.run(client.preflight())
        document = {
            "availability": {
                "available": preflight.available,
                "transport": preflight.transport.value,
                "upstream_version": preflight.upstream_version,
                "reason": preflight.reason,
            },
            "mcp": preflight.model_dump(mode="json"),
            "reasoning_ready": reasoning_ready,
            "reasoning_reason": reasoning_reason,
            "key_values_printed": False,
        }
        # MCP evidence collection has no Vibe LLM requirement. Report optional
        # reasoning readiness, but do not make it part of transport compatibility.
        exit_code = 0 if preflight.available and preflight.compatible else 1
    elif args.command == "agent-check":
        ready, reason = settings.agent_reasoning_readiness()
        document = {
            "available": ready,
            "reason": reason,
            "provider": settings.agent_provider,
            "model": settings.agent_model,
            "key_values_printed": False,
        }
        exit_code = 0 if ready else 1
    elif args.command == "learning-audit":
        audit = audit_learning_readiness(settings.workflow_artifact_dir)
        document = audit.model_dump(mode="json")
        # Insufficient data is an expected shadow-mode result, not an
        # operational command failure.
        exit_code = 0
    print(json.dumps(document, indent=2))
    return exit_code


def _provider_smoke(
    args: argparse.Namespace, settings: EventLensSettings
) -> dict[str, Any]:
    """Run one quota-conscious provider call and return a secret-safe summary."""

    registry = build_free_first_registry(settings)
    provider = getattr(registry, args.provider)
    if provider is None:
        raise ValueError(f"{args.provider} is not configured; check .env")

    ticker = str(args.ticker).strip().upper()
    if not ticker:
        raise ValueError("--ticker must not be blank")

    capability = args.capability
    if capability == "earnings-events":
        start = args.start or date.today()
        end = args.end or (start + timedelta(days=31))
        response = provider.get_earnings_events(ticker, start, end)
        events = response.data.events
        result: dict[str, Any] = {
            "event_count": len(events),
            "events_preview": [
                {
                    "event_date": event.event_date.isoformat(),
                    "event_release_at": (
                        event.event_release_at.isoformat()
                        if event.event_release_at is not None
                        else None
                    ),
                    "fiscal_quarter": event.fiscal_quarter,
                    "fiscal_period_end": (
                        event.fiscal_period_end.isoformat()
                        if event.fiscal_period_end is not None
                        else None
                    ),
                    "timing": event.timing.value,
                    "source_event_id": event.source_event_id,
                    "estimated_eps": (
                        str(event.estimated_eps)
                        if event.estimated_eps is not None
                        else None
                    ),
                    "reported_eps": (
                        str(event.reported_eps)
                        if event.reported_eps is not None
                        else None
                    ),
                }
                for event in events[:5]
            ],
        }
    elif capability == "company-news":
        if args.start is None or args.end is None:
            raise ValueError("--start and --end are required for company-news")
        response = provider.get_company_news(
            ticker, args.start, args.end, cutoff_at=args.cutoff_at
        )
        articles = response.data.articles
        labels: dict[str, int] = {}
        for article in articles:
            label = article.ticker_sentiment_label or "missing"
            labels[label] = labels.get(label, 0) + 1
        result = {
            "article_count": len(articles),
            "sentiment_labels": dict(sorted(labels.items())),
            "articles_preview": [
                {
                    "provider_article_id": article.provider_article_id,
                    "headline": article.headline,
                    "url": article.url,
                    "published_at": article.published_at.isoformat(),
                    "ticker_sentiment_label": article.ticker_sentiment_label,
                }
                for article in articles[:5]
            ],
        }
    elif capability == "transcript":
        if args.provider != "alpha_vantage":
            raise ValueError(
                "transcript smoke is Alpha Vantage-only in the free-first MVP; "
                "Finnhub transcripts are premium"
            )
        response = provider.get_transcript(ticker, args.quarter)
        passages = response.data.passages
        result = {
            "quarter": response.data.fiscal_quarter,
            "event_date": (
                response.data.event_date.isoformat()
                if response.data.event_date is not None
                else None
            ),
            "passage_count": len(passages),
            "first_passage_preview": passages[0].text[:240] if passages else None,
        }
    elif capability == "daily-history":
        if args.provider != "alpha_vantage":
            raise ValueError(
                "daily-history smoke is Alpha Vantage-only; select that provider explicitly"
            )
        if args.start is None or args.end is None:
            raise ValueError(
                "--start and --end are required for daily-history to avoid "
                "accidental quota use"
            )
        response = provider.get_daily_history(ticker, args.start, args.end)
        bars = response.data.bars
        result = {
            "bar_count": len(bars),
            "coverage_start": bars[0].trading_date.isoformat() if bars else None,
            "coverage_end": bars[-1].trading_date.isoformat() if bars else None,
            "first_close": str(bars[0].close) if bars else None,
            "last_close": str(bars[-1].close) if bars else None,
        }
    else:  # defensive; argparse owns normal validation.
        raise ValueError(f"unsupported capability: {capability}")

    return {
        "provider": args.provider,
        "capability": capability,
        "ticker": ticker,
        "result": result,
        "provenance": _provider_provenance_summary(response.provenance),
        "key_values_printed": False,
    }


def _provider_provenance_summary(provenance: Any) -> dict[str, Any]:
    return {
        "provider_name": provenance.provider_name,
        "provider_version": provenance.provider_version,
        "origin": provenance.origin.value,
        "retrieved_at": provenance.retrieved_at.isoformat(),
        "coverage_start": (
            provenance.coverage_start.isoformat()
            if provenance.coverage_start is not None
            else None
        ),
        "coverage_end": (
            provenance.coverage_end.isoformat()
            if provenance.coverage_end is not None
            else None
        ),
        "quota_consumed": provenance.quota_consumed,
        "data_quality": provenance.data_quality.value,
        "raw_response_sha256": provenance.raw_response_sha256,
        "warnings": [
            {"code": warning.code, "message": warning.message}
            for warning in provenance.warnings
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
