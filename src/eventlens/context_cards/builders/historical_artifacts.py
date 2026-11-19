"""Approval and immutable persistence for reviewed historical context cards."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eventlens.artifacts.models import sha256_hex
from eventlens.context_cards.evidence import EvidenceClass, EvidenceReceipt
from eventlens.context_cards.rendering import (
    ContextCardArtifacts,
    write_context_card_artifacts,
)

from .historical import (
    HistoricalContextCardArtifact,
    HistoricalNewsEvidenceBinding,
    ManagementGuidanceAvailability,
)

_STALE_HEADLINE_ONLY_CAVEAT = (
    "Massive news evidence is limited to rights-safe headline metadata and "
    "provider-native categorical labels; no numeric relevance or sentiment value "
    "was inferred."
)
_PRIVATE_SUMMARY_CAVEAT = (
    "Massive descriptions and ticker-specific reasoning are retained only in the "
    "private summary resolver under a versioned grant; they are provider summaries, "
    "not article bodies, and no numeric relevance or sentiment value was inferred."
)


class HistoricalContextCardArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_json_path: Path
    readable_markdown_path: Path
    artifact_json_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    readable_markdown_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical: ContextCardArtifacts


def bind_historical_news_evidence(
    artifact: HistoricalContextCardArtifact,
    binding: HistoricalNewsEvidenceBinding,
) -> HistoricalContextCardArtifact:
    """Create a reviewed replay revision bound to immutable resolver manifests."""

    if artifact.status != "reviewed" or artifact.news_evidence_binding is not None:
        raise ValueError("news evidence binds exactly once to a reviewed card")
    bridge = binding.semantic_reaction_bridge
    if bridge is not None:
        if bridge.ticker != artifact.ticker:
            raise ValueError("semantic reaction bridge must match the card ticker")
        reactions = artifact.canonical_card.market_reaction_history
        if not reactions:
            raise ValueError("semantic reaction bridge requires a canonical reaction")
        adjusted = reactions[0].benchmark_adjusted_returns
        expected_t1 = adjusted.t_plus_1_pct if adjusted is not None else None
        expected_t5 = adjusted.t_plus_5_pct if adjusted is not None else None
        if (
            bridge.benchmark_adjusted_t1 != expected_t1
            or bridge.benchmark_adjusted_t5 != expected_t5
        ):
            raise ValueError(
                "semantic reaction bridge must match benchmark-adjusted returns"
            )
    receipt = EvidenceReceipt(
        id=f"historical-news-evidence:{artifact.event_id}",
        evidence_class=EvidenceClass.RECONSTRUCTED,
        claim=(
            f"Retrospective resolver retained {binding.selected_passage_count} "
            f"passage(s) and {len(binding.selected_provider_summaries)} selected "
            f"provider-summary reference(s), with "
            f"{len(binding.provider_summary_pulls)} private direct pull(s), across "
            f"{binding.item_count} item(s); "
            f"semantic status is {binding.semantic_status}."
        ),
        source_ref=(
            f"eventlens-private-news-evidence:{binding.private_manifest_sha256}"
        ),
    )
    card = artifact.canonical_card
    if any(item.id == receipt.id for item in card.evidence_registry.receipts):
        raise ValueError("historical news evidence receipt already exists")
    caveat = (
        "Historical news resolver manifests were reconstructed after the target "
        "event from pre-cutoff provider responses. Permitted provider summaries "
        "remain private and are not article bodies or causal evidence."
    )
    corrected_caveats = tuple(
        _PRIVATE_SUMMARY_CAVEAT if item == _STALE_HEADLINE_ONLY_CAVEAT else item
        for item in artifact.data_quality_caveats
    )
    corrected_warnings = tuple(
        _PRIVATE_SUMMARY_CAVEAT if item == _STALE_HEADLINE_ONLY_CAVEAT else item
        for item in card.quality_control.warnings
    )
    card = card.model_copy(
        update={
            "evidence_registry": card.evidence_registry.model_copy(
                update={"receipts": (*card.evidence_registry.receipts, receipt)}
            ),
            "quality_control": card.quality_control.model_copy(
                update={
                    "warnings": tuple(
                        dict.fromkeys((*corrected_warnings, caveat))
                    )
                }
            ),
            "provenance": card.provenance.model_copy(
                update={
                    "source_manifest_sha256": sha256_hex(
                        {
                            "prior": card.provenance.source_manifest_sha256,
                            "private_news": binding.private_manifest_sha256,
                            "public_news": binding.public_projection_sha256,
                        }
                    )
                }
            ),
        }
    )
    payload = artifact.model_dump(mode="python")
    payload.update(
        {
            "news_evidence_binding": binding,
            "data_quality_caveats": tuple(
                dict.fromkeys((*corrected_caveats, caveat))
            ),
            "provenance_links": tuple(
                dict.fromkeys(
                    (
                        *artifact.provenance_links,
                        f"eventlens-private-news-evidence:{binding.private_manifest_sha256}",
                        f"eventlens-public-news-evidence:{binding.public_projection_sha256}",
                    )
                )
            ),
            "content_sha256": sha256_hex(card.model_dump(mode="json")),
            "canonical_card": card,
        }
    )
    return HistoricalContextCardArtifact.model_validate(payload)


def approve_historical_context_card(
    artifact: HistoricalContextCardArtifact,
    *,
    approved_at: datetime,
    approved_by: str,
) -> HistoricalContextCardArtifact:
    """Apply an explicit owner approval; deterministic review cannot call this."""

    if artifact.status != "reviewed":
        raise ValueError("only a reviewed historical card can be approved")
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise ValueError("approved_at must be timezone-aware")
    approved_at = approved_at.astimezone(UTC)
    if approved_at >= artifact.target_cutoff_at:
        raise ValueError("historical-card approval must precede the target cutoff")
    if not approved_by.strip():
        raise ValueError("approved_by must not be blank")
    approved = artifact.canonical_card.model_copy(
        update={
            "identity": artifact.canonical_card.identity.model_copy(
                update={
                    "card": artifact.canonical_card.identity.card.model_copy(
                        update={"status": "approved"}
                    ),
                }
            ),
            "quality_control": artifact.canonical_card.quality_control.model_copy(
                update={
                    "publication_status": "approved",
                    "requires_human_review": False,
                }
            ),
        }
    )
    payload = artifact.model_dump(mode="python")
    payload.update(
        {
            "status": "approved",
            "available_at": approved_at,
            "approved_at": approved_at,
            "approved_by": approved_by.strip(),
            "content_sha256": sha256_hex(approved.model_dump(mode="json")),
            "canonical_card": approved,
        }
    )
    return HistoricalContextCardArtifact.model_validate(payload)


def revise_historical_guidance_availability(
    artifact: HistoricalContextCardArtifact,
    availability: ManagementGuidanceAvailability,
) -> HistoricalContextCardArtifact:
    """Create one reviewed immutable revision with explicit guidance availability."""

    if artifact.status != "reviewed":
        raise ValueError("guidance availability can revise only a reviewed artifact")
    if artifact.management_guidance_availability is not None:
        raise ValueError("management-guidance availability is already explicit")
    return HistoricalContextCardArtifact.model_validate(
        {
            **artifact.model_dump(mode="python"),
            "management_guidance_availability": availability,
        }
    )


def write_historical_context_card_artifacts(
    artifact: HistoricalContextCardArtifact,
    root: Path,
) -> HistoricalContextCardArtifacts:
    """Write immutable readable and canonical artifacts for one card revision."""

    root = Path(root).resolve()
    canonical = write_context_card_artifacts(
        artifact.canonical_card, root / "canonical"
    )
    revision_sha256 = artifact.content_sha256
    if artifact.management_guidance_availability is not None:
        revision_sha256 = sha256_hex(
            {
                "canonical_card_sha256": artifact.content_sha256,
                "management_guidance_availability": (
                    artifact.management_guidance_availability.model_dump(mode="json")
                ),
            }
        )
    revision = f"{artifact.event_date.isoformat()}-{revision_sha256[:16]}"
    directory = root / artifact.ticker / artifact.card_id / revision
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "historical-context-card.json"
    markdown_path = directory / "historical-context-card.md"
    json_bytes = artifact.canonical_bytes()
    markdown_bytes = (_readable_markdown(artifact) + "\n").encode("utf-8")
    _write_immutable(json_path, json_bytes)
    _write_immutable(markdown_path, markdown_bytes)
    return HistoricalContextCardArtifacts(
        artifact_json_path=json_path,
        readable_markdown_path=markdown_path,
        artifact_json_sha256=hashlib.sha256(json_bytes).hexdigest(),
        readable_markdown_sha256=hashlib.sha256(markdown_bytes).hexdigest(),
        canonical=canonical,
    )


def _readable_markdown(artifact: HistoricalContextCardArtifact) -> str:
    messages = "\n".join(f"- {message}" for message in artifact.key_messages)
    caveats = "\n".join(f"- {caveat}" for caveat in artifact.data_quality_caveats)
    sources = "\n".join(f"- {source}" for source in artifact.provenance_links)
    identity = artifact.canonical_card.identity
    event_at = identity.event.event_datetime
    actuals = _actual_metrics(artifact)
    limitations = _known_limitations(artifact)
    news = artifact.native_news_summary
    news_section = ""
    if news is not None:
        labels = ", ".join(
            f"{label}={news.sentiment_label_counts.get(label, 0)}"
            for label in ("positive", "neutral", "negative", "unknown")
        )
        news_section = (
            "## Reconstructed pre-event news metadata\n\n"
            f"Massive returned {news.query_article_count} ticker-associated headline(s); "
            f"the deterministic issuer-name rule retained {news.issuer_named_article_count} "
            f"and excluded {news.excluded_not_issuer_named_count}. Provider-native labels "
            f"for retained rows: {labels}. No numeric sentiment score was inferred.\n\n"
        )
    eps = artifact.reconstructed_eps_context
    eps_section = ""
    if eps is not None:
        eps_section = (
            "## Retrospective EPS cross-check\n\n"
            f"Alpha Vantage currently reports estimated EPS `{eps.estimated_eps}` and "
            f"reported EPS `{eps.reported_eps}`. This row was retrieved after the event "
            "and is excluded from surprise computation and backtest features.\n\n"
        )
    binding = artifact.news_evidence_binding
    evidence_section = ""
    if binding is not None:
        evidence_section = (
            "## Private semantic-evidence coverage\n\n"
            f"Semantic status: `{binding.semantic_status}`. Resolver coverage: "
            f"{binding.item_count} item(s), {binding.content_available_count} "
            f"content-available, {binding.summary_only_count} summary-only, "
            f"{binding.headline_only_count} headline-only, "
            f"{binding.selected_passage_count} selected passage(s), and "
            f"{len(binding.selected_provider_summaries)} selected provider-summary "
            f"reference(s). Post-event explanation gate: "
            f"`{binding.post_event_explanation_gate.status}`. The private "
            f"manifest is `{binding.private_manifest_sha256}` and its public-safe "
            f"projection is `{binding.public_projection_sha256}`.\n\n"
        )
        if binding.provider_summary_pulls:
            evidence_section += _provider_summary_section(binding)
    reaction_bridge_section = ""
    if binding is not None and binding.semantic_reaction_bridge is not None:
        reaction_bridge_section = (
            "### Qualitative–quantitative bridge\n\n"
            f"{binding.semantic_reaction_bridge.interpretation}\n\n"
        )
    return (
        f"# Historical context — {artifact.ticker} {artifact.event_id}\n\n"
        "> Decision-neutral prior-event context. Not investment advice.\n\n"
        "## Identity and review state\n\n"
        f"- Company: {identity.company}\n"
        f"- Fiscal period: {identity.event.fiscal_period}\n"
        f"- Event datetime: {event_at.isoformat() if event_at else 'unknown'}\n"
        f"- Historical information cutoff: "
        f"{artifact.canonical_card.temporal_state.historical_event_cutoff.isoformat()}\n"
        f"- Card status: `{artifact.status}`; human review required: "
        f"`{artifact.canonical_card.quality_control.requires_human_review}`\n"
        f"- Citation: `{artifact.citation_uri}`\n\n"
        f"## Executive summary\n\n{artifact.executive_summary}\n\n"
        f"## Key messages\n\n{messages}\n\n"
        f"## Reported event facts\n\n{actuals}\n\n"
        f"## Metric interpretation\n\n{artifact.metric_interpretation}\n\n"
        f"{news_section}"
        f"{evidence_section}"
        f"## Reaction interpretation\n\n{artifact.reaction_interpretation}\n\n"
        f"{reaction_bridge_section}"
        f"{eps_section}"
        f"## Known limitations and intentionally omitted fields\n\n{limitations}\n\n"
        f"## Caveats\n\n{caveats}\n\n"
        f"## Provenance\n\n{sources}\n\n"
        "## Canonical audit support\n\n"
        "The canonical v4 JSON and Markdown are persisted separately for schema "
        "validation and audit. The generic decision-template rendering is not "
        "repeated in this historical human view because its empty decision, "
        "scenario, and current-state sections are not historical findings.\n\n"
        f"- Canonical normalized JSON SHA-256: "
        f"`{artifact.canonical_card.identity.card.sha256 or 'recorded in separate canonical artifact'}`\n"
        f"- Bound canonical-card SHA-256: `{artifact.content_sha256}`"
    )


def _actual_metrics(artifact: HistoricalContextCardArtifact) -> str:
    quarters = artifact.canonical_card.historical_quarters
    if not quarters:
        return "- No issuer actuals were retained."
    rows = []
    for metric, value in sorted(
        quarters[0].actual_results.metrics.items(), key=lambda item: item[0].value
    ):
        if value is None:
            rows.append(f"- {metric.value}: unavailable")
            continue
        rows.append(
            f"- {metric.value}: {value.display_value()} "
            f"(`{value.accounting_basis.value}`, `{value.scope.value}`)"
        )
    return "\n".join(rows)


def _known_limitations(artifact: HistoricalContextCardArtifact) -> str:
    card = artifact.canonical_card
    rows = [
        f"- Sell-side expectations: `{card.consensus_state.status}`; "
        + "; ".join(card.consensus_state.blockers),
        _guidance_limitation(artifact),
        "- Decision, entry threshold, and scenario outputs: intentionally omitted from this decision-neutral historical card.",
    ]
    missing = artifact.pre_event_features.get("missing_values", [])
    if isinstance(missing, list):
        for item in missing:
            if isinstance(item, dict) and item.get("field") and item.get("reason"):
                rows.append(f"- {item['field']}: {item['reason']}")
    return "\n".join(rows)


def _guidance_limitation(artifact: HistoricalContextCardArtifact) -> str:
    state = artifact.management_guidance_availability
    if state is None:
        return "- Prior management guidance: availability state is missing."
    if state.status == "available":
        return (
            "- Prior management guidance: available in the canonical guidance ledger."
        )
    return f"- Prior management guidance: `{state.status}`; {state.reason}"


def _provider_summary_section(binding: HistoricalNewsEvidenceBinding) -> str:
    has_post_event = any(
        pull.reference.phase.value == "post_event"
        for pull in binding.provider_summary_pulls
    )
    heading = (
        "## Selected semantic evidence\n"
        if has_post_event
        else "## Selected pre-event semantic evidence\n"
    )
    sections = [
        heading,
        (
            "These are bounded direct pulls from Massive's provider descriptions and "
            "ticker-specific reasoning. They are private summary evidence—not article "
            "bodies, independent verification, forecasts, or causal explanations.\n"
        ),
    ]
    for index, pull in enumerate(binding.provider_summary_pulls, start=1):
        reference = pull.reference
        direct_quote = "\n".join(
            f"> {line}" if line else ">"
            for line in pull.direct_text.replace("\r\n", "\n").split("\n")
        )
        sections.extend(
            (
                f"### Selected summary {index}\n",
                (
                    f"- Headline: {pull.headline}\n"
                    f"- Source: {pull.source_name or pull.source_domain or 'unknown'}\n"
                    f"- Source URL: {pull.source_url}\n"
                    f"- Published: {reference.published_at.isoformat()}\n"
                    f"- Phase: `{reference.phase.value}`\n"
                    f"- Provider-native label: "
                    f"`{reference.provider_native_sentiment_label or 'unknown'}`\n"
                    f"- Themes: {', '.join(pull.themes)}\n"
                    f"- Summary checksum: `{reference.summary_sha256}`\n"
                ),
                f"**Direct provider text**\n\n{direct_quote}\n",
                (
                    "**Why it is surfaced after the event:** "
                    if reference.phase.value == "post_event"
                    else "**Why it mattered entering earnings:** "
                )
                + f"{pull.why_it_mattered}\n",
                f"**Question seed:** {pull.question_seed}\n",
            )
        )
    return "\n".join(sections) + "\n"


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f"immutable artifact differs: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise FileExistsError(f"immutable artifact differs: {path}") from None
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "HistoricalContextCardArtifacts",
    "approve_historical_context_card",
    "bind_historical_news_evidence",
    "revise_historical_guidance_availability",
    "write_historical_context_card_artifacts",
]
