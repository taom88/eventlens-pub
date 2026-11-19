"""Versioned prompt-level skills available to the analyst interpreter."""

from __future__ import annotations

from .models import AgentSkill


DEFAULT_SKILLS = (
    AgentSkill(
        skill_id="evidence_synthesis",
        description=(
            "Synthesize relationships across all relevant supplied passages, distinguishing "
            "management statements from analyst interpretation and citing every factual or "
            "interpretive claim."
        ),
    ),
    AgentSkill(
        skill_id="hypothesis_assessment",
        description=(
            "Assess each base, bull, and bear hypothesis component against supporting, "
            "contradicting, and missing evidence without rewriting the analyst's hypothesis."
        ),
    ),
    AgentSkill(
        skill_id="historical_pattern_interpretation",
        description=(
            "Interpret patterns in EventLens-supplied results, separate recurring signals from "
            "one-off observations, and never perform or replace canonical arithmetic."
        ),
    ),
    AgentSkill(
        skill_id="reaction_forecast_vs_action",
        description=(
            "Report market-reaction forecasts separately from portfolio action. State the "
            "signed and absolute move, horizon, uncertainty, and tradeability; a correct "
            "reaction forecast must not automatically become a BUY, TRIM, or SELL."
        ),
    ),
    AgentSkill(
        skill_id="market_reaction_measurement",
        description=(
            "Measure after-hours movement separately from the next-session open, T+1 close, "
            "and T+5 close. For after-close events, use next-session open versus the "
            "pre-event reference close as the primary actionable reaction and preserve the "
            "timing basis and missing-data warnings."
        ),
    ),
    AgentSkill(
        skill_id="hold_decision_calibration",
        description=(
            "When the forecast is positive or negative but the action is HOLD, explain the "
            "entry threshold, expected move after costs, uncertainty, current-price gap, "
            "and the observable condition that would change the action."
        ),
    ),
    AgentSkill(
        skill_id="missed_opportunity_postmortem",
        description=(
            "Score forecast accuracy and decision quality independently after outcomes. "
            "Report forecast error, realized tradeability, avoided downside, and any "
            "missed opportunity without relabeling a good forecast as a bad one."
        ),
    ),
    AgentSkill(
        skill_id="caveat_identification",
        description=(
            "Identify evidence gaps, conflicts, stale or degraded inputs, alternative "
            "explanations, and the specific observations that would resolve uncertainty."
        ),
    ),
    AgentSkill(
        skill_id="historical_context_gate",
        description=(
            "Before any real, trial, replay, or backtest hypothesis, retrieve the issuer's "
            "cutoff-eligible prior event context cards (prefer three, retain up to twelve), "
            "record their IDs/checksums, and refuse to proceed silently when they are missing. "
            "Never expose the target event outcome or post-cutoff evidence before the lock."
        ),
    ),
    AgentSkill(
        skill_id="human_readable_context_cards",
        description=(
            "When surfacing a context card, provide an executive summary, key messages, "
            "plain-language interpretation of the metrics, and explicit data-quality and "
            "rights caveats. Treat IDs, checksums, and raw JSON as audit support rather than "
            "the human-facing card content."
        ),
    ),
    AgentSkill(
        skill_id="follow_up_drilldown",
        description=(
            "Answer the exact follow-up from the retained evidence bundle while using the "
            "approved prior report and locked hypothesis as continuity context; add useful "
            "detail, implications, tensions, and caveats without merely repeating the brief."
        ),
    ),
    AgentSkill(
        skill_id="historical_reaction_citation",
        description=(
            "Cite historical-card claims using eventlens-context-card:<card_id> with an "
            "executive-summary or key-message passage. Treat historical cards as descriptive "
            "evidence, not direct recommendations, and never cite an unavailable passage."
        ),
    ),
    AgentSkill(
        skill_id="historical_context_descriptive_guard",
        description=(
            "Use historical cards as descriptive, sample-size- and regime-aware evidence only. "
            "Do not infer a direct trade action from a prior reaction, and disclose run-up, "
            "valuation, volatility, and missing-context limitations."
        ),
    ),
    AgentSkill(
        skill_id="context_card_citation_discipline",
        description=(
            "Cite only supplied eventlens-context-card:<card_id> executive-summary or "
            "key-message passages; reject spoofed, unavailable, post-cutoff, or uncited "
            "historical-card claims."
        ),
    ),
)

# These descriptors carry their stricter typed contract in ``agents.news``;
# registering the prompt-level names here makes them available to the existing
# allowlist mechanism without making a network/model call.
from .news import NEWS_AGENT_SKILLS

DEFAULT_SKILLS = (*DEFAULT_SKILLS, *NEWS_AGENT_SKILLS)
