"""Translate a plain-language research request into a reproducible run plan.

This is the product boundary: provider collection and model reasoning remain
separate, so a downloaded installation can swap DeepSeek/Kimi/another
OpenAI-compatible model without changing the evidence contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from eventlens.config import EventLensSettings
from eventlens.agents import (
    run_configured_agent_route,
)
from eventlens.product.research_evidence import ResearchEvidenceBundle
from eventlens.product.synthesis_input import external_bundle_from_research_evidence


class ResearchAskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    status: str
    stage: str
    model_provider: str
    model: str
    cards: tuple[dict[str, str], ...] = ()
    reasoning: dict[str, object] | None = None
    locked_replay: dict[str, object] | None = None
    next_action: str
    created_at: datetime


def render_research_ask_markdown(payload: ResearchAskResult | dict[str, object]) -> str:
    """Render a human-readable companion without changing canonical data."""

    data = (
        payload.model_dump(mode="json")
        if isinstance(payload, ResearchAskResult)
        else payload
    )
    ticker = str(data.get("ticker", "UNKNOWN"))
    lines = [
        f"# EventLens Pre-Earnings Research — {ticker}",
        "",
        "> Research-only decision support. The JSON artifact remains canonical.",
        "",
        "## Run status",
        "",
        f"- Session: `{data.get('session_id', 'unknown')}`",
        f"- Status: `{data.get('status', 'unknown')}`",
        f"- Stage: `{data.get('stage', 'unknown')}`",
        f"- Model: `{data.get('model_provider', 'unknown')}/{data.get('model', 'unknown')}`",
        f"- Created: `{data.get('created_at', 'unknown')}`",
        "",
        "## Analyst request",
        "",
        str(data.get("question", "Unavailable")),
    ]
    locked = data.get("locked_replay")
    if isinstance(locked, dict):
        hypothesis = locked.get("hypothesis") if isinstance(locked.get("hypothesis"), dict) else {}
        decision = locked.get("decision") if isinstance(locked.get("decision"), dict) else {}
        lines.extend([
            "",
            "## Deterministic locked hypothesis",
            "",
            f"- Event: `{locked.get('event_id', 'unknown')}`",
            f"- Information cutoff: `{locked.get('information_cutoff_at', locked.get('locked_at', 'unknown'))}`",
            f"- Expected release: `{locked.get('event_release_at', 'unknown')}`",
            f"- Context gate: `{locked.get('context_gate_status', 'unknown')}`",
            f"- Base case: {hypothesis.get('base_case', 'Unavailable')}",
            f"- Bull case: {hypothesis.get('bull_case', 'Unavailable')}",
            f"- Bear case: {hypothesis.get('bear_case', 'Unavailable')}",
            f"- Expected signed reaction: `{hypothesis.get('expected_signed_reaction_pct', 'unavailable')}`",
            f"- Confidence: `{hypothesis.get('confidence', 'unavailable')}`",
            "",
            "### Decision thresholds",
            "",
            f"- Stance: **{str(decision.get('stance', 'unavailable')).upper()}**",
            f"- Buy below: `{decision.get('buy_below', 'unavailable')}`",
            f"- Trim above: `{decision.get('trim_above', 'unavailable')}`",
            f"- Sell above: `{decision.get('sell_above', 'unavailable')}`",
            "",
            "### Deterministic rationale",
            "",
        ])
        rationale = decision.get("rationale", [])
        lines.extend(f"- {item}" for item in rationale if isinstance(item, str))
    reasoning = data.get("reasoning")
    lines.extend(["", "## Model interpretation", ""])
    if isinstance(reasoning, dict) and isinstance(reasoning.get("report"), dict):
        report = reasoning["report"]
        lines.extend([
            f"### {report.get('headline', 'Evidence-grounded report')}",
            "",
            str(report.get("executive_summary", "Unavailable")),
            "",
            "### Claims",
            "",
        ])
        for claim in report.get("claims", []):
            if isinstance(claim, dict):
                citations = ", ".join(
                    f"`{item.get('source_id')}#{item.get('passage_id')}`"
                    for item in claim.get("citations", []) if isinstance(item, dict)
                )
                lines.append(f"- {claim.get('text', 'Unavailable')} ({citations})")
    else:
        lines.append("No validated model report was produced.")
        if isinstance(reasoning, dict):
            for error in reasoning.get("errors", []):
                if isinstance(error, dict):
                    lines.append(
                        f"- `{error.get('code', 'error')}`: {error.get('message', 'Unavailable')}"
                    )
    lines.extend(["", "## Historical context lineage", ""])
    for card in data.get("cards", []):
        if isinstance(card, dict):
            lines.append(
                f"- `{card.get('event_id', 'unknown')}` — `{card.get('status', 'unknown')}` — "
                f"SHA `{card.get('sha256', 'unknown')}`"
            )
    lines.extend([
        "",
        "## Next action",
        "",
        str(data.get("next_action", "Unavailable")),
        "",
    ])
    return "\n".join(lines)


def write_research_markdown(payload: dict[str, object], output: Path) -> tuple[Path, Path, str]:
    """Atomically write Markdown and its byte-level checksum."""

    content = render_research_ask_markdown(payload)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(output)
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum_tmp = checksum.with_suffix(checksum.suffix + ".tmp")
    checksum_tmp.write_text(digest + "\n", encoding="utf-8")
    checksum_tmp.replace(checksum)
    return output, checksum, digest


_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,5}(?:\.[A-Z]{1,2})?(?![A-Za-z0-9])")


def infer_ticker(
    question: str, *, issuer_aliases: Mapping[str, str] | None = None
) -> str:
    """Infer an explicit symbol, with optional aliases supplied by an instance.

    Issuer aliases are data owned by the configured issuer/entity resolver; the
    central product path deliberately contains no company-specific mapping.
    """

    text = question.strip()
    if not text:
        raise ValueError("research question must not be blank")
    upper = text.upper()
    aliases = issuer_aliases or {}
    for name, ticker in sorted(aliases.items(), key=lambda item: (-len(item[0]), item[0])):
        if name in upper:
            return ticker.upper()
    # Symbols must be explicit uppercase tokens.  Uppercasing the whole prose
    # would misclassify ordinary words such as PREPARE or STOCK as tickers.
    candidates = [item for item in _TICKER_RE.findall(text) if item not in {"I", "A"}]
    if not candidates:
        raise ValueError("could not infer a ticker; include an explicit market symbol")
    return candidates[0]


def run_research_ask(
    question: str,
    *,
    settings: EventLensSettings,
    output_dir: Path,
    session_id: str | None = None,
    evidence_bundle: ResearchEvidenceBundle | None = None,
    ticker: str | None = None,
    issuer_aliases: Mapping[str, str] | None = None,
) -> ResearchAskResult:
    """Run the universal product path from one immutable facts bundle.

    Issuer instances collect and normalize evidence before this boundary.  The
    shared path never calls an issuer-specific builder and never selects safety
    policy by ticker.
    """

    requested_ticker = (ticker or infer_ticker(question, issuer_aliases=issuer_aliases)).upper()
    run_id = session_id or f"{requested_ticker.lower()}-ask-{uuid4().hex[:10]}"
    if evidence_bundle is None:
        return ResearchAskResult(
            question=question.strip(), ticker=requested_ticker, session_id=run_id,
            status="blocked_evidence_missing", stage="research_evidence",
            model_provider=settings.agent_route().value,
            model=("user_supplied" if settings.external_agent_enabled else settings.agent_model),
            reasoning={
                "route": settings.agent_route().value, "report": None,
                "errors": [{
                    "code": "research_evidence_missing",
                    "message": "No validated ResearchEvidenceBundle was supplied by the issuer instance.",
                }],
            },
            next_action="Build and validate the issuer research-evidence bundle; no recommendation was generated.",
            created_at=datetime.now(timezone.utc),
        )
    facts = ResearchEvidenceBundle.model_validate(evidence_bundle.model_dump(mode="json"))
    if facts.target.ticker != requested_ticker:
        raise ValueError("research evidence ticker does not match the requested ticker")
    insufficient_history = facts.newly_covered_issuer is not None
    if len(facts.prior_cards) < 3 and not insufficient_history:
        return ResearchAskResult(
            question=question.strip(), ticker=requested_ticker, session_id=run_id,
            status="blocked_context_missing", stage="historical_context_gate",
            model_provider=settings.agent_route().value,
            model=("user_supplied" if settings.external_agent_enabled else settings.agent_model),
            reasoning={
                "route": settings.agent_route().value, "report": None,
                "errors": [{
                    "code": "blocked_context_missing",
                    "message": "Three cutoff-eligible approved prior cards are required unless an explicit newly-covered issuer eligibility receipt is supplied.",
                }],
            },
            cards=tuple({
                "card_id": item.card_id, "event_id": item.prior_event_id,
                "status": item.card.identity.card.status, "sha256": item.card_sha256,
            } for item in facts.prior_cards),
            next_action="Build eligible approved prior cards or supply a typed newly-covered issuer eligibility receipt.",
            created_at=datetime.now(timezone.utc),
        )
    external = external_bundle_from_research_evidence(
        facts, question=question, session_id=run_id, route=settings.agent_route(),
    )
    import asyncio
    handoff = asyncio.run(run_configured_agent_route(
        external, settings=settings, artifact_root=output_dir / "external-agent",
    ))
    report = (
        json.loads(Path(handoff.report_path).read_text(encoding="utf-8"))
        if handoff.report_path is not None else None
    )
    status = (
        "awaiting_external_agent"
        if handoff.status.value == "awaiting_external_agent" else "reasoning_ready"
    )
    return ResearchAskResult(
        question=question.strip(), ticker=requested_ticker, session_id=run_id,
        status=status,
        stage=("awaiting_external_agent" if status == "awaiting_external_agent" else "pre_earnings_reasoning"),
        model_provider=handoff.route.value,
        model=("user_supplied" if settings.external_agent_enabled else settings.agent_model),
        cards=tuple({
            "card_id": item.card_id,
            "event_id": item.prior_event_id,
            "status": item.card.identity.card.status,
            "sha256": item.card_sha256,
        } for item in facts.prior_cards),
        reasoning={
            "route": handoff.route.value, "bundle_id": handoff.bundle_id,
            "bundle_sha256": handoff.bundle_sha256,
            "research_evidence_bundle_id": facts.bundle_id,
            "research_evidence_sha256": facts.bundle_sha256,
            "handoff_status": handoff.status.value, "report": report, "errors": [],
        },
        locked_replay={
            "status": "evidence_locked",
            "event_id": facts.target.event_id,
            "information_cutoff_at": facts.information_cutoff_at,
            "event_release_at": facts.target.event_at,
            "context_gate_status": (
                "passed" if len(facts.prior_cards) >= 3 else "insufficient_history"
            ),
            "research_evidence_sha256": facts.bundle_sha256,
        },
        next_action=(
            "Import the typed external-agent report."
            if status == "awaiting_external_agent"
            else "Review the validated report. This remains research-only output."
        ),
        created_at=datetime.now(timezone.utc),
    )


__all__ = [
    "ResearchAskResult", "infer_ticker", "run_research_ask",
    "render_research_ask_markdown", "write_research_markdown",
]
