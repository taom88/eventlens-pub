"""Deterministic, non-inferential Markdown rendering for v4 cards."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import ContextCardV4

_UNKNOWN = "unknown / not computed"


class ContextCardArtifacts(BaseModel):
    """Immutable JSON and Markdown artifacts for one normalized-card revision."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    json_path: Path
    markdown_path: Path
    json_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    markdown_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def render_context_card_markdown(card: ContextCardV4 | Mapping[str, Any]) -> str:
    """Render the prescribed 16 sections from the canonical JSON representation."""
    _, payload = _normalized(card)
    return _render(payload, _sha256(_json_bytes(payload)))


def write_context_card_artifacts(card: ContextCardV4 | Mapping[str, Any], root: Path) -> ContextCardArtifacts:
    """Atomically publish revision-addressed artifacts without overwriting history."""
    parsed, payload = _normalized(card)
    json_bytes = _json_bytes(payload)
    json_sha256 = _sha256(json_bytes)
    markdown_bytes = (_render(payload, json_sha256) + "\n").encode()
    revision = f"{parsed.identity.card.generated_at.strftime('%Y%m%dT%H%M%S%fZ')}-{json_sha256[:16]}"
    directory = Path(root).resolve() / _safe(parsed.identity.ticker) / _safe(parsed.identity.event.fiscal_period) / revision
    directory.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = directory / "context-card-v4.json", directory / "context-card-v4.md"
    _atomic_immutable_write(json_path, json_bytes)
    _atomic_immutable_write(markdown_path, markdown_bytes)
    return ContextCardArtifacts(json_path=json_path, markdown_path=markdown_path, json_sha256=json_sha256, markdown_sha256=_sha256(markdown_bytes))


def _normalized(card: ContextCardV4 | Mapping[str, Any]) -> tuple[ContextCardV4, dict[str, Any]]:
    parsed = card if isinstance(card, ContextCardV4) else ContextCardV4.model_validate(card)
    return parsed, parsed.model_dump(mode="json")


def _render(p: Mapping[str, Any], digest: str) -> str:
    i, c, qc = p["identity"], p["current_pre_event_state"], p["quality_control"]
    dc, consensus, thesis = p["decision_context"], p["consensus_state"], p["thesis_state"]
    lines = [
        f"# EventLens Context Card — {i['ticker']} {i['event']['fiscal_period']}", "",
        "> Canonical, decision-neutral research context. Not investment advice.", "",
        "## 1. Card identity", "", f"- Company: {i['company']}",
        f"- Event: {i['event']['type']} / {i['event']['fiscal_period']}",
        f"- Event datetime: {_v(i['event']['event_datetime'])} ({i['event']['event_datetime_status']})",
        f"- As of: {i['card']['as_of']}", f"- Generated at: {i['card']['generated_at']}",
        f"- Normalized JSON SHA-256: `{digest}`", "",
        "## 2. Approval / QC state", "", f"- Card status: {i['card']['status']}",
        f"- Publication status: {qc['publication_status']}", f"- Human review required: {qc['requires_human_review']}", "",
        "## 3. Executive decision context", "", f"- Type: {dc['decision_type']}",
        f"- Horizon: {_v(dc['horizon_start'])} to {_v(dc['horizon_end'])}",
        f"- Reference price: {_number(dc['reference_price'])}", f"- Entry threshold: {_number(dc['entry_threshold'])}",
        f"- Decision status: {dc['decision_status']}", "",
        "## 4. Current pre-event state", "", f"- Latest reported fiscal period: {_v(c['latest_reported_fiscal_period'])}", *_metrics(c['latest_actuals']),
        *_items("Capacity state", {"value": c['capacity_state']}), *_items("AI state", c['ai_state']), "",
        "### Tier-1 ontology observations", "", *_tier_1(p['tier_1_observations']), "",
        "## 5. Current expectations", "", f"- Status: {consensus['status']}", f"- Fiscal period: {_v(consensus['fiscal_period'])}", *_metrics(consensus['metrics']), *_bullets("Blocker", consensus['blockers']), "",
        "## 6. Current management guidance", "", *_metrics(c['current_management_guidance']), *_items("Guidance", c['current_management_guidance_qualitative']), "",
        "## 7. Primary unresolved questions", "", *_questions(thesis['unresolved']), "",
        "## 8. Last four quarter comparison", "", "| Quarter | Event timestamp evidence | Actual metrics | Sell-side expectations | Prior management guidance | AH return | T+1 return |", "| --- | --- | --- | --- | --- | --- | --- |", *_history(p['historical_quarters']), "",
        "## 9. Guidance ledger", "", *_guidance(p['management_guidance_ledger']), "",
        "## 10. Market reaction patterns", "", *_reactions(p['market_reaction_history']), "",
        "## 11. Accounting adjustments", "", *_accounting(c['earnings_quality']), "",
        "## 12. Scenario state", "", f"- Status: {p['scenario_model']['status']}", *_scenarios(p['scenario_model']), "",
        "## 13. Decision", "", f"- Status: {p['decision']['status']}", "- Long-term view explicitly separate: true", "",
        "## 14. Evidence receipts", "", *_receipts(p['evidence_registry']['receipts']), "",
        "## 15. Data-quality warnings", "", *_bullets("Blocker", qc['blockers']), *_bullets("Warning", qc['warnings']), "",
        "## 16. Provenance", "", f"- Schema version: {p['schema_version']}", f"- Ontology version: {p['provenance']['ontology_version']}", f"- Pipeline commit: {_v(p['provenance']['pipeline_commit'])}", f"- Normalized-card provenance SHA-256: {_v(p['provenance']['normalized_card_sha256'])}", f"- Rendered normalized JSON SHA-256: `{digest}`",
    ]
    return "\n".join(lines)


def _v(value: Any) -> str: return _UNKNOWN if value is None else str(value)
def _number(value: Mapping[str, Any]) -> str: return str(value['value']) if value['value'] is not None else value['status'].replace('_', ' ')
def _period(p: Mapping[str, Any]) -> str: return f"FY{p['fiscal_year']}" + ("" if p['fiscal_quarter'] is None else f" Q{p['fiscal_quarter']}")
def _metric(x: Mapping[str, Any] | None) -> str:
    if x is None or x['value'] is None: return _UNKNOWN
    return f"{x['value']} {x['unit']} ({_period(x['fiscal_period'])}; {x['accounting_basis']}; {x['growth_basis']})"
def _metrics(values: Mapping[str, Any]) -> list[str]: return [f"- {k}: {_metric(v)}" for k, v in sorted(values.items())] or [f"- Metrics: {_UNKNOWN}"]
def _tier_1(items: list[Mapping[str, Any]]) -> list[str]:
    return [f"- {x['view']} / {x['fiscal_period']} / {x['ontology_key']}: {_tier_1_value(x)}" for x in items] or [f"- {_UNKNOWN}"]
def _tier_1_value(x: Mapping[str, Any]) -> str:
    if x['value'] is None:
        return "missing — " + x['missing_reason']
    if x.get('original_value') is not None or x.get('original_unit') is not None or x.get('conversion_provenance') is not None:
        return (
            f"canonical: {x['value']} {_v(x.get('unit'))}; "
            f"original: {x['original_value']} {_v(x.get('original_unit'))}; "
            f"basis: {_v(x.get('basis'))}; "
            f"conversion: {_v(x.get('conversion_provenance'))}"
        )
    return f"{x['value']} {_v(x.get('unit'))} (basis: {_v(x.get('basis'))})"
def _items(label: str, values: Mapping[str, Any]) -> list[str]:
    if not values or set(values) == {'value'} and values['value'] is None: return [f"- {label}: {_UNKNOWN}"]
    return [f"- {label} — {k}: {_v(v)}" for k, v in sorted(values.items())]
def _bullets(label: str, values: list[Any]) -> list[str]: return [f"- {label}: {x}" for x in values] or [f"- {label}: none recorded"]
def _questions(items: list[Mapping[str, Any]]) -> list[str]: return [f"- {x['question']} ({x['importance']})" for x in items] or [f"- {_UNKNOWN}"]
def _summary(values: Mapping[str, Any]) -> str: return "; ".join(f"{k}: {_metric(v)}" for k, v in sorted(values.items())) or _UNKNOWN
def _history(quarters: list[Mapping[str, Any]]) -> list[str]:
    rows = []
    for q in quarters[:4]:
        e = {x['consensus_type']: x for x in q['expectations']}; r = q['reaction']
        returns = {} if r is None else r['returns']
        guides = e.get('prior_management_guidance', {}).get('prior_guidance', [])
        rows.append(f"| {_period(q['fiscal_period'])} | {_timestamp_proxy(q.get('event_timestamp_evidence'))} | {_summary(q['actual_results']['metrics'])} | {_summary(e.get('sell_side', {}).get('metrics', {}))} | {'; '.join(x['guidance_id'] for x in guides) or _UNKNOWN} | {_v(returns.get('after_hours_pct'))} | {_v(returns.get('t_plus_1_pct'))} |")
    return rows + [f"| {_UNKNOWN} | {_UNKNOWN} | {_UNKNOWN} | {_UNKNOWN} | {_UNKNOWN} | {_UNKNOWN} | {_UNKNOWN} |"] * (4 - len(rows))
def _timestamp_proxy(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return _UNKNOWN
    return (
        f"{value['first_verified_at']} "
        f"({value['timestamp_kind']}; SEC acceptance proxy; receipt {value['acceptance_receipt']}; "
        f"policy {value['policy_version']})"
    )
def _guidance(items: list[Mapping[str, Any]]) -> list[str]: return [f"- {x['guidance_id']}: {x['metric']} → {_period(x['target_fiscal_period'])} ({x['guidance']['type']})" for x in items] or [f"- {_UNKNOWN}"]
def _reactions(items: list[Mapping[str, Any]]) -> list[str]: return [f"- {x['fiscal_period']}: AH {_v(x['returns']['after_hours_pct'])}; T+1 {_v(x['returns']['t_plus_1_pct'])}; T+5 {_v(x['returns']['t_plus_5_pct'])}" for x in items] or [f"- {_UNKNOWN}"]
def _accounting(quality: Mapping[str, Any] | None) -> list[str]:
    if quality is None: return [f"- {_UNKNOWN}"]
    adjustments = [
        adjustment
        for mapping in quality['normalization_mappings']
        for adjustment in mapping['adjustments']
    ]
    return [f"- {x['name']}: {_metric(x['eps_effect'])}" for x in adjustments] or ["- No adjustments recorded"]
def _scenarios(s: Mapping[str, Any]) -> list[str]: return [f"- {n}: probability {_number(s[n]['probability'])}; expected return {_number(s[n]['expected_post_event_return'])}" for n in ('bull', 'base', 'bear')]
def _receipts(items: list[Mapping[str, Any]]) -> list[str]: return [f"- `{x['id']}` [{x['evidence_class']}]: {x['claim']} (source: {_v(x['source_ref'])})" for x in items] or [f"- {_UNKNOWN}"]
def _json_bytes(p: Mapping[str, Any]) -> bytes: return (json.dumps(p, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
def _sha256(value: bytes) -> str: return hashlib.sha256(value).hexdigest()
def _safe(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not safe: raise ValueError("artifact identity does not contain a safe path segment")
    return safe[:120]
def _atomic_immutable_write(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content: raise FileExistsError(f"immutable artifact already exists with different content: {path}")
        return
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent); temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        try: os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != content: raise FileExistsError(f"immutable artifact already exists with different content: {path}")
    finally: temp.unlink(missing_ok=True)

__all__ = ["ContextCardArtifacts", "render_context_card_markdown", "write_context_card_artifacts"]
