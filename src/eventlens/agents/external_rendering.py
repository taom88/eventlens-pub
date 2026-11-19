"""Deterministic human-readable rendering for validated external reports."""

from __future__ import annotations

from .external import ExternalAgentReport


def render_external_report_markdown(report: ExternalAgentReport) -> str:
    lines = [
        f"# EventLens External-Agent Report — {report.event_id}",
        "",
        "> Research-only interpretation. The validated JSON artifact remains canonical.",
        "",
        "## Recommendation",
        "",
    ]
    if report.recommendation is None:
        lines.append(f"- Abstained: {report.abstention.reason if report.abstention else 'unspecified'}")
    else:
        recommendation = report.recommendation
        lines.extend([
            f"- Action: **{recommendation.action.value}**",
            f"- Conviction: `{recommendation.conviction}`",
            f"- Thesis: {recommendation.thesis}",
            f"- Counter-thesis: {recommendation.counter_thesis}",
        ])
        lines.extend(f"- Invalidation: {item}" for item in recommendation.invalidations)
        lines.extend(f"- Condition to change: {item}" for item in recommendation.conditions_to_change)
    lines.extend(["", "## Scenarios", ""])
    for scenario in report.scenarios:
        lines.append(
            f"- **{scenario.scenario_id.upper()}** ({scenario.probability}): {scenario.thesis}"
            + _citations(scenario.citations)
        )
    if report.reaction_forecast is not None:
        forecast = report.reaction_forecast
        lines.extend([
            "", "## Reaction forecast", "",
            f"- Signed move: `{forecast.signed_move_pct}`",
            f"- Absolute move: `{forecast.absolute_move_pct}`",
            f"- Horizon: `{forecast.horizon}`",
            f"- Uncertainty: {forecast.uncertainty}",
            f"- Tradeability: {forecast.tradeability}",
        ])
    lines.extend(["", "## Evidence-backed claims", ""])
    lines.extend(
        f"- {claim.text}{_citations(claim.citations)}" for claim in report.claims
    )
    lines.extend(["", "## Valuation zones", ""])
    if report.valuation_zones:
        for zone in report.valuation_zones:
            lines.append(
                f"- `{zone.zone_id}`: {zone.currency} {zone.lower}–{zone.upper} "
                f"using {zone.method} ({zone.basis}){_citations(zone.citations)}"
            )
    else:
        lines.append("No validated valuation zone was supplied.")
    lines.extend(["", "## Warnings and receipt", ""])
    lines.extend(f"- {warning}" for warning in report.warnings)
    lines.extend([
        f"- Producer: `{report.usage.provider}/{report.usage.model}`",
        f"- Prompt version: `{report.usage.prompt_version}`",
        f"- Input/output tokens: `{report.usage.input_tokens}` / `{report.usage.output_tokens}`",
        f"- Bundle: `{report.bundle_id}` revision `{report.bundle_revision}` SHA `{report.bundle_sha256}`",
        "",
    ])
    return "\n".join(lines)


def _citations(citations) -> str:
    if not citations:
        return ""
    return " " + " ".join(
        f"`[{item.source_id}#passage:{item.passage_id}]`" for item in citations
    )


__all__ = ["render_external_report_markdown"]
