"""Build readable, decision-neutral prior-event context-card artifacts.

The builder joins already-retained issuer facts, cutoff-safe pre-event features,
and deterministic market reactions. It does not fetch providers, infer missing
consensus, approve a card, or make a recommendation.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.context_cards.evidence import (
    AnalyticalUse,
    EvidenceClass,
    EvidenceReceipt,
)
from eventlens.context_cards.history import (
    ActualResults,
    ConsensusType,
    Expectation,
    HistoricalQuarter,
    SurpriseLayers,
)
from eventlens.context_cards.metrics import FiscalPeriod, MetricValue
from eventlens.context_cards.models import ContextCardV4
from eventlens.context_cards.qc import PublicationStatus, validate_card
from eventlens.context_cards.reactions import (
    REACTION_METHODOLOGY_VERSION,
    MarketReaction,
)
from eventlens.product.live_news_evidence import (
    LiveNewsEvidenceManifest,
    ProviderSummaryReference,
)
from eventlens.product.research_evidence import (
    EvidenceReceipt as ProductEvidenceReceipt,
)
from eventlens.product.research_evidence import (
    PriorContextCardEvidence,
)
from eventlens.quant.pre_event_features import PreEventFeatureSnapshot

HISTORICAL_CARD_ARTIFACT_VERSION = "eventlens.historical-context-card.v1"
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
SummaryTheme = Literal[
    "capacity_and_demand",
    "financial_expectations",
    "partnership_or_contract",
    "risk_or_regulation",
    "market_positioning",
    "other_issuer_context",
]

_SUMMARY_THEME_PATTERNS: tuple[tuple[SummaryTheme, re.Pattern[str]], ...] = (
    (
        "capacity_and_demand",
        re.compile(r"\b(?:capacity|data cent(?:er|re)|gpu|cloud|demand|utilization)\b", re.IGNORECASE),
    ),
    (
        "financial_expectations",
        re.compile(r"\b(?:revenue|earnings|guidance|estimate|profit|loss|margin|ebitda)\b", re.IGNORECASE),
    ),
    (
        "partnership_or_contract",
        re.compile(r"\b(?:contract|partnership|agreement|deal|customer)\b", re.IGNORECASE),
    ),
    (
        "risk_or_regulation",
        re.compile(
            r"\b(?:risk|investigation|regulatory|lawsuit|sanction|probe|capex|"
            r"capital expenditure|funding|financing|debt|cash burn)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "market_positioning",
        re.compile(r"\b(?:competition|market share|analyst|price target|rating|upgrade|downgrade)\b", re.IGNORECASE),
    ),
)

_SUMMARY_THEME_CONTEXT: dict[SummaryTheme, tuple[str, str]] = {
    "capacity_and_demand": (
        "This was a pre-event seed for testing whether reported capacity, utilization, or demand confirmed the incoming narrative.",
        "What did the results and call reveal about capacity delivery, utilization, and demand conversion?",
    ),
    "financial_expectations": (
        "This was a pre-event seed for comparing reported financials and guidance with the expectations narrative.",
        "Which reported metrics or guidance most clearly confirmed or contradicted the incoming financial narrative?",
    ),
    "partnership_or_contract": (
        "This was a pre-event seed for checking whether announced commercial relationships translated into measurable operating progress.",
        "Did management quantify the timing, economics, or capacity implications of the referenced commercial relationship?",
    ),
    "risk_or_regulation": (
        "This was a pre-event seed for checking whether a named risk changed management commentary, guidance, or execution evidence.",
        "Did the results or call confirm, narrow, or disprove the pre-event risk described here?",
    ),
    "market_positioning": (
        "This was a pre-event seed for testing the market-positioning narrative against issuer-reported evidence.",
        "Which reported facts supported or contradicted the pre-event market-positioning claim?",
    ),
    "other_issuer_context": (
        "This issuer-specific summary was retained as a question seed, not as a verified prediction or article-body claim.",
        "What issuer-reported evidence confirmed or contradicted this pre-event summary?",
    ),
}

_POST_EVENT_CONTEXT = (
    (
        "This post-event provider summary is surfaced only after a triggered "
        "divergence as an explanation candidate, not as causal proof."
    ),
    (
        "Which retained results, guidance, and market evidence supports or "
        "contradicts this candidate explanation?"
    ),
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class MissingValue(_Frozen):
    field: NonEmptyStr
    reason: NonEmptyStr


class NativeNewsLabelSummary(_Frozen):
    """Bounded, provider-native categorical news context without invented scores."""

    provider: Literal["massive"] = "massive"
    provider_version: NonEmptyStr
    query_article_count: int = Field(ge=0)
    issuer_named_article_count: int = Field(ge=0)
    excluded_not_issuer_named_count: int = Field(ge=0)
    sentiment_label_counts: dict[
        Literal["positive", "neutral", "negative", "unknown"], int
    ]
    selection_method: Literal["headline_issuer_name_or_ticker_v1"] = (
        "headline_issuer_name_or_ticker_v1"
    )
    raw_response_sha256: Sha256
    retrieved_at: datetime

    @model_validator(mode="after")
    def counts_and_time_are_valid(self) -> NativeNewsLabelSummary:
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("news retrieval time must be timezone-aware")
        if (
            self.issuer_named_article_count + self.excluded_not_issuer_named_count
            != self.query_article_count
        ):
            raise ValueError("news selection counts must reconcile to the query count")
        if sum(self.sentiment_label_counts.values()) != self.issuer_named_article_count:
            raise ValueError(
                "news sentiment-label counts must reconcile to retained rows"
            )
        return self


class ReconstructedEpsContext(_Frozen):
    """Retrospective provider row barred from surprise/backtest use."""

    provider: Literal["alpha_vantage"] = "alpha_vantage"
    provider_version: NonEmptyStr
    estimated_eps: Decimal | None = None
    reported_eps: Decimal | None = None
    raw_response_sha256: Sha256
    retrieved_at: datetime
    analytical_status: Literal["retrospective_descriptive_only"] = (
        "retrospective_descriptive_only"
    )

    @model_validator(mode="after")
    def values_and_time_are_valid(self) -> ReconstructedEpsContext:
        if self.estimated_eps is None and self.reported_eps is None:
            raise ValueError("reconstructed EPS context requires at least one value")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("EPS retrieval time must be timezone-aware")
        return self


class PostEventExplanationGate(_Frozen):
    """Deterministic gate preventing routine post-event hindsight from surfacing."""

    policy_version: Literal["historical-post-event-explanation-v1"] = (
        "historical-post-event-explanation-v1"
    )
    status: Literal[
        "unavailable_no_comparison_baseline",
        "unavailable_no_reaction",
        "not_triggered_small_reaction",
        "not_triggered_aligned",
        "triggered_divergence",
    ]
    material_reaction_threshold: Decimal = Field(default=Decimal("0.10"), gt=0)
    expected_reaction_direction: Literal["positive", "negative"] | None = None
    fundamental_surprise_direction: Literal["positive", "negative"] | None = None
    observed_benchmark_adjusted_t1: Decimal | None = None

    @model_validator(mode="after")
    def status_matches_inputs(self) -> PostEventExplanationGate:
        baselines = tuple(
            value
            for value in (
                self.expected_reaction_direction,
                self.fundamental_surprise_direction,
            )
            if value is not None
        )
        if not baselines:
            expected = "unavailable_no_comparison_baseline"
        elif self.observed_benchmark_adjusted_t1 is None:
            expected = "unavailable_no_reaction"
        elif (
            abs(self.observed_benchmark_adjusted_t1)
            < self.material_reaction_threshold
        ):
            expected = "not_triggered_small_reaction"
        else:
            observed = (
                "positive"
                if self.observed_benchmark_adjusted_t1 > 0
                else "negative"
            )
            expected = (
                "triggered_divergence"
                if any(value != observed for value in baselines)
                else "not_triggered_aligned"
            )
        if self.status != expected:
            raise ValueError("post-event explanation gate status does not match inputs")
        return self


def evaluate_post_event_explanation_gate(
    *,
    observed_benchmark_adjusted_t1: Decimal | None,
    expected_reaction_direction: Literal["positive", "negative"] | None = None,
    fundamental_surprise_direction: Literal["positive", "negative"] | None = None,
    material_reaction_threshold: Decimal = Decimal("0.10"),
) -> PostEventExplanationGate:
    """Surface hindsight only for a material, directionally contrary outcome."""

    baselines = tuple(
        value
        for value in (expected_reaction_direction, fundamental_surprise_direction)
        if value is not None
    )
    common = {
        "material_reaction_threshold": material_reaction_threshold,
        "expected_reaction_direction": expected_reaction_direction,
        "fundamental_surprise_direction": fundamental_surprise_direction,
        "observed_benchmark_adjusted_t1": observed_benchmark_adjusted_t1,
    }
    if not baselines:
        return PostEventExplanationGate(
            status="unavailable_no_comparison_baseline", **common
        )
    if observed_benchmark_adjusted_t1 is None:
        return PostEventExplanationGate(status="unavailable_no_reaction", **common)
    if abs(observed_benchmark_adjusted_t1) < material_reaction_threshold:
        return PostEventExplanationGate(
            status="not_triggered_small_reaction", **common
        )
    observed_direction = (
        "positive" if observed_benchmark_adjusted_t1 > 0 else "negative"
    )
    return PostEventExplanationGate(
        status=(
            "triggered_divergence"
            if any(value != observed_direction for value in baselines)
            else "not_triggered_aligned"
        ),
        **common,
    )


class HistoricalProviderSummaryPull(_Frozen):
    """Private direct text plus bounded deterministic context for one summary."""

    reference: ProviderSummaryReference
    headline: NonEmptyStr
    source_name: NonEmptyStr | None = None
    source_domain: NonEmptyStr | None = None
    source_url: NonEmptyStr
    direct_text: str = Field(min_length=1, max_length=10_000)
    themes: tuple[SummaryTheme, ...] = Field(min_length=1)
    why_it_mattered: NonEmptyStr
    question_seed: NonEmptyStr

    @model_validator(mode="after")
    def text_and_context_match_reference(self) -> HistoricalProviderSummaryPull:
        expected = sha256_hex(
            {
                "article_metadata_sha256": self.reference.article_metadata_sha256,
                "summary": self.direct_text,
            }
        )
        if self.reference.summary_sha256 != expected:
            raise ValueError("provider-summary direct text does not match its reference")
        canonical_themes = tuple(
            theme
            for theme, pattern in _SUMMARY_THEME_PATTERNS
            if pattern.search(self.direct_text)
        ) or ("other_issuer_context",)
        if self.themes != canonical_themes:
            raise ValueError("provider-summary themes must be canonical and text-derived")
        expected_context = (
            _POST_EVENT_CONTEXT
            if self.reference.phase.value == "post_event"
            else _SUMMARY_THEME_CONTEXT[self.themes[0]]
        )
        if (self.why_it_mattered, self.question_seed) != expected_context:
            raise ValueError("provider-summary context must match its primary theme")
        return self


def build_historical_provider_summary_pulls(
    manifest: LiveNewsEvidenceManifest,
    references: tuple[ProviderSummaryReference, ...],
) -> tuple[HistoricalProviderSummaryPull, ...]:
    """Resolve selected summaries without copying arbitrary provider responses."""

    by_id = {
        item.article.provider_article_id: item.article for item in manifest.items
    }
    pulls: list[HistoricalProviderSummaryPull] = []
    for reference in references:
        article = by_id.get(reference.article_id)
        if article is None:
            raise ValueError("provider-summary reference is absent from the manifest")
        direct_text = manifest.resolve_provider_summary(reference)
        themes = tuple(
            theme
            for theme, pattern in _SUMMARY_THEME_PATTERNS
            if pattern.search(direct_text)
        ) or ("other_issuer_context",)
        why_it_mattered, question_seed = (
            _POST_EVENT_CONTEXT
            if reference.phase.value == "post_event"
            else _SUMMARY_THEME_CONTEXT[themes[0]]
        )
        pulls.append(
            HistoricalProviderSummaryPull(
                reference=reference,
                headline=article.headline,
                source_name=article.source_name,
                source_domain=article.source_domain,
                source_url=article.url,
                direct_text=direct_text,
                themes=themes,
                why_it_mattered=why_it_mattered,
                question_seed=question_seed,
            )
        )
    return tuple(pulls)


class HistoricalSemanticReactionBridge(_Frozen):
    """Checksum-bound comparison of pre-event summary signals and price reaction."""

    policy_version: Literal["historical-semantic-reaction-bridge-v1"] = (
        "historical-semantic-reaction-bridge-v1"
    )
    ticker: NonEmptyStr
    selected_summary_sha256s: tuple[Sha256, ...] = Field(min_length=1, max_length=10)
    provider_label_counts: dict[
        Literal["positive", "neutral", "negative", "unknown"], int
    ]
    themes: tuple[SummaryTheme, ...] = Field(min_length=1)
    positive_themes: tuple[SummaryTheme, ...] = ()
    negative_themes: tuple[SummaryTheme, ...] = ()
    benchmark_adjusted_t1: Decimal | None = None
    benchmark_adjusted_t5: Decimal | None = None
    label_tilt: Literal["positive", "negative", "balanced", "unclassified"]
    directional_relationship: Literal[
        "aligned", "divergent", "mixed_or_unclassified", "unavailable_reaction"
    ]
    interpretation: NonEmptyStr

    @model_validator(mode="after")
    def derived_fields_match_inputs(self) -> HistoricalSemanticReactionBridge:
        expected_keys = {"positive", "neutral", "negative", "unknown"}
        if set(self.provider_label_counts) != expected_keys or any(
            value < 0 for value in self.provider_label_counts.values()
        ):
            raise ValueError("provider label counts must contain non-negative canonical keys")
        if sum(self.provider_label_counts.values()) != len(
            self.selected_summary_sha256s
        ):
            raise ValueError("provider label counts must match selected summaries")
        if len(set(self.selected_summary_sha256s)) != len(
            self.selected_summary_sha256s
        ):
            raise ValueError("semantic reaction summaries cannot repeat")
        if len(set(self.themes)) != len(self.themes):
            raise ValueError("semantic reaction themes cannot repeat")
        expected_tilt = _provider_label_tilt(self.provider_label_counts)
        expected_relationship = _directional_relationship(
            expected_tilt, self.benchmark_adjusted_t1
        )
        if self.label_tilt != expected_tilt:
            raise ValueError("semantic reaction label tilt does not match counts")
        if self.directional_relationship != expected_relationship:
            raise ValueError("semantic reaction relationship does not match inputs")
        expected_interpretation = _semantic_reaction_interpretation(
            ticker=self.ticker,
            label_counts=self.provider_label_counts,
            themes=self.themes,
            positive_themes=self.positive_themes,
            negative_themes=self.negative_themes,
            label_tilt=self.label_tilt,
            relationship=self.directional_relationship,
            benchmark_adjusted_t1=self.benchmark_adjusted_t1,
            benchmark_adjusted_t5=self.benchmark_adjusted_t5,
        )
        if self.interpretation != expected_interpretation:
            raise ValueError("semantic reaction interpretation is not deterministic")
        return self


def build_historical_semantic_reaction_bridge(
    *,
    ticker: str,
    pulls: tuple[HistoricalProviderSummaryPull, ...],
    reaction: MarketReaction,
) -> HistoricalSemanticReactionBridge:
    """Relate text-derived pre-event themes to returns without claiming causality."""

    pre_event_pulls = tuple(
        pull for pull in pulls if pull.reference.phase.value == "pre_event"
    )
    if not pre_event_pulls:
        raise ValueError("semantic reaction bridge requires pre-event summary evidence")
    label_counts = _provider_label_counts(pre_event_pulls)
    themes = tuple(
        dict.fromkeys(theme for pull in pre_event_pulls for theme in pull.themes)
    )
    positive_themes = _themes_for_label(pre_event_pulls, "positive")
    negative_themes = _themes_for_label(pre_event_pulls, "negative")
    adjusted = reaction.benchmark_adjusted_returns
    t1 = adjusted.t_plus_1_pct if adjusted is not None else None
    t5 = adjusted.t_plus_5_pct if adjusted is not None else None
    tilt = _provider_label_tilt(label_counts)
    relationship = _directional_relationship(tilt, t1)
    normalized_ticker = ticker.strip().upper()
    return HistoricalSemanticReactionBridge(
        ticker=normalized_ticker,
        selected_summary_sha256s=tuple(
            pull.reference.summary_sha256 for pull in pre_event_pulls
        ),
        provider_label_counts=label_counts,
        themes=themes,
        positive_themes=positive_themes,
        negative_themes=negative_themes,
        benchmark_adjusted_t1=t1,
        benchmark_adjusted_t5=t5,
        label_tilt=tilt,
        directional_relationship=relationship,
        interpretation=_semantic_reaction_interpretation(
            ticker=normalized_ticker,
            label_counts=label_counts,
            themes=themes,
            positive_themes=positive_themes,
            negative_themes=negative_themes,
            label_tilt=tilt,
            relationship=relationship,
            benchmark_adjusted_t1=t1,
            benchmark_adjusted_t5=t5,
        ),
    )


def _provider_label_counts(
    pulls: tuple[HistoricalProviderSummaryPull, ...],
) -> dict[Literal["positive", "neutral", "negative", "unknown"], int]:
    counts: dict[Literal["positive", "neutral", "negative", "unknown"], int] = {
        "positive": 0,
        "neutral": 0,
        "negative": 0,
        "unknown": 0,
    }
    for pull in pulls:
        raw_label = pull.reference.provider_native_sentiment_label
        label = raw_label.strip().lower() if raw_label is not None else "unknown"
        canonical = label if label in {"positive", "neutral", "negative"} else "unknown"
        counts[canonical] += 1  # type: ignore[literal-required]
    return counts


def _themes_for_label(
    pulls: tuple[HistoricalProviderSummaryPull, ...],
    label: Literal["positive", "negative"],
) -> tuple[SummaryTheme, ...]:
    return tuple(
        dict.fromkeys(
            theme
            for pull in pulls
            if (pull.reference.provider_native_sentiment_label or "").strip().lower()
            == label
            for theme in pull.themes
        )
    )


def _provider_label_tilt(
    counts: dict[Literal["positive", "neutral", "negative", "unknown"], int],
) -> Literal["positive", "negative", "balanced", "unclassified"]:
    if counts["positive"] > counts["negative"]:
        return "positive"
    if counts["negative"] > counts["positive"]:
        return "negative"
    if counts["positive"] or counts["negative"]:
        return "balanced"
    return "unclassified"


def _directional_relationship(
    label_tilt: Literal["positive", "negative", "balanced", "unclassified"],
    t1: Decimal | None,
) -> Literal[
    "aligned", "divergent", "mixed_or_unclassified", "unavailable_reaction"
]:
    if t1 is None:
        return "unavailable_reaction"
    if label_tilt not in {"positive", "negative"} or t1 == 0:
        return "mixed_or_unclassified"
    return "aligned" if (t1 > 0) == (label_tilt == "positive") else "divergent"


def _semantic_reaction_interpretation(
    *,
    ticker: str,
    label_counts: dict[
        Literal["positive", "neutral", "negative", "unknown"], int
    ],
    themes: tuple[SummaryTheme, ...],
    positive_themes: tuple[SummaryTheme, ...],
    negative_themes: tuple[SummaryTheme, ...],
    label_tilt: Literal["positive", "negative", "balanced", "unclassified"],
    relationship: Literal[
        "aligned", "divergent", "mixed_or_unclassified", "unavailable_reaction"
    ],
    benchmark_adjusted_t1: Decimal | None,
    benchmark_adjusted_t5: Decimal | None,
) -> str:
    theme_text = _human_join(tuple(_human_theme(theme) for theme in themes))
    count_text = ", ".join(
        f"{label_counts[label]} {label}"
        for label in ("positive", "neutral", "negative", "unknown")
    )
    if label_tilt == "positive":
        narrative = "a positively tilted incoming narrative"
    elif label_tilt == "negative":
        narrative = "a negatively tilted incoming narrative"
    elif label_tilt == "balanced":
        narrative = "a directionally balanced incoming narrative"
    else:
        narrative = "an incoming narrative without a classified directional tilt"
    emphasis_parts = []
    if positive_themes:
        emphasis_parts.append(
            "positively labeled summaries emphasized "
            + _human_join(tuple(_human_theme(theme) for theme in positive_themes))
        )
    if negative_themes:
        emphasis_parts.append(
            "negatively labeled summaries emphasized "
            + _human_join(tuple(_human_theme(theme) for theme in negative_themes))
        )
    emphasis = (
        f" Within that set, {'; '.join(emphasis_parts)}."
        if emphasis_parts
        else ""
    )
    opening = (
        f"The selected pre-event provider-summary text centered on {theme_text}."
        f"{emphasis} Its provider-native labels were {count_text}, forming {narrative}."
    )
    if benchmark_adjusted_t1 is None:
        comparison = (
            " A benchmark-adjusted T+1 return was unavailable, so the qualitative "
            "signals could not be compared with the first full-session reaction."
        )
    else:
        direction = "positive" if benchmark_adjusted_t1 > 0 else "negative"
        magnitude = _reaction_magnitude(benchmark_adjusted_t1)
        if relationship == "aligned":
            relation = "directionally aligned with"
        elif relationship == "divergent":
            relation = "directionally contrary to"
        else:
            relation = "not comparable to a clear directional tilt in"
        comparison = (
            f" {ticker}'s {_format_pct(benchmark_adjusted_t1)} benchmark-adjusted "
            f"T+1 move was a {magnitude} {direction} response and was {relation} "
            "that label balance."
        )
    if benchmark_adjusted_t5 is None:
        persistence = " The benchmark-adjusted T+5 reading was unavailable."
    else:
        persistence = (
            f" The {_format_pct(benchmark_adjusted_t5)} benchmark-adjusted T+5 "
            "reading shows the excess-return level remaining through the fifth close."
        )
    guardrail = (
        " This comparison makes the selected themes explicit hypotheses for checking "
        "against reported results and call evidence. Provider labels are not return "
        "forecasts, and neither the summaries nor the price path establishes why the "
        "stock moved."
    )
    return opening + comparison + persistence + guardrail


def _reaction_magnitude(value: Decimal) -> str:
    absolute = abs(value)
    if absolute >= Decimal("0.20"):
        return "very large"
    if absolute >= Decimal("0.10"):
        return "large"
    if absolute >= Decimal("0.05"):
        return "material"
    return "modest"


def _format_pct(value: Decimal) -> str:
    return f"{value * 100:+.2f}%"


def _human_join(values: tuple[str, ...]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _human_theme(theme: SummaryTheme) -> str:
    return {
        "capacity_and_demand": "capacity and demand",
        "financial_expectations": "financial expectations",
        "partnership_or_contract": "partnerships or contracts",
        "risk_or_regulation": "capital, funding, or regulatory risk",
        "market_positioning": "market positioning",
        "other_issuer_context": "other issuer context",
    }[theme]


class HistoricalNewsEvidenceBinding(_Frozen):
    """Compact pointer from a card to its private and public news manifests."""

    private_manifest_id: NonEmptyStr
    private_manifest_sha256: Sha256
    public_projection_sha256: Sha256
    source_retrieved_at: datetime
    reconstructed_at: datetime
    item_count: int = Field(ge=0)
    content_available_count: int = Field(ge=0)
    summary_only_count: int = Field(default=0, ge=0)
    headline_only_count: int = Field(ge=0)
    selected_passage_count: int = Field(ge=0)
    selected_provider_summaries: tuple[ProviderSummaryReference, ...] = Field(
        default=(), max_length=10
    )
    provider_summary_pulls: tuple[HistoricalProviderSummaryPull, ...] = Field(
        default=(), max_length=10
    )
    semantic_reaction_bridge: HistoricalSemanticReactionBridge | None = None
    post_event_explanation_gate: PostEventExplanationGate = Field(
        default_factory=lambda: evaluate_post_event_explanation_gate(
            observed_benchmark_adjusted_t1=None
        )
    )
    semantic_status: Literal[
        "passage_backed", "summary_backed", "abstained_no_eligible_passages"
    ]

    @model_validator(mode="after")
    def counts_match_status(self) -> HistoricalNewsEvidenceBinding:
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.source_retrieved_at, self.reconstructed_at)
        ):
            raise ValueError(
                "historical news binding timestamps must be timezone-aware"
            )
        if self.source_retrieved_at > self.reconstructed_at:
            raise ValueError("historical news source cannot follow reconstruction")
        if (
            self.content_available_count
            + self.summary_only_count
            + self.headline_only_count
            != self.item_count
        ):
            raise ValueError("historical news evidence counts must reconcile")
        has_passages = self.selected_passage_count > 0
        has_summaries = bool(self.selected_provider_summaries)
        if len({item.article_id for item in self.selected_provider_summaries}) != len(
            self.selected_provider_summaries
        ):
            raise ValueError("selected provider summaries cannot repeat articles")
        if self.provider_summary_pulls and tuple(
            item.reference for item in self.provider_summary_pulls
        ) != self.selected_provider_summaries:
            raise ValueError(
                "resolved provider-summary pulls must match selected references"
            )
        if self.semantic_reaction_bridge is not None:
            pre_event_pulls = tuple(
                item
                for item in self.provider_summary_pulls
                if item.reference.phase.value == "pre_event"
            )
            if not pre_event_pulls:
                raise ValueError(
                    "semantic reaction bridge requires resolved pre-event pulls"
                )
            expected_sha256s = tuple(
                item.reference.summary_sha256 for item in pre_event_pulls
            )
            if (
                self.semantic_reaction_bridge.selected_summary_sha256s
                != expected_sha256s
            ):
                raise ValueError("semantic reaction bridge must bind selected summaries")
            expected_themes = tuple(
                dict.fromkeys(
                    theme for item in pre_event_pulls for theme in item.themes
                )
            )
            if self.semantic_reaction_bridge.themes != expected_themes:
                raise ValueError("semantic reaction bridge themes must match summary text")
            if self.semantic_reaction_bridge.positive_themes != _themes_for_label(
                pre_event_pulls, "positive"
            ) or self.semantic_reaction_bridge.negative_themes != _themes_for_label(
                pre_event_pulls, "negative"
            ):
                raise ValueError(
                    "semantic reaction bridge label-specific themes must match summary text"
                )
            if self.semantic_reaction_bridge.provider_label_counts != _provider_label_counts(
                pre_event_pulls
            ):
                raise ValueError("semantic reaction bridge labels must match references")
        expected_status = (
            "passage_backed"
            if has_passages
            else "summary_backed"
            if has_summaries
            else "abstained_no_eligible_passages"
        )
        if self.semantic_status != expected_status:
            raise ValueError(
                "historical news semantic status must match retained evidence"
            )
        if any(
            item.phase.value == "post_event"
            for item in self.selected_provider_summaries
        ) and self.post_event_explanation_gate.status != "triggered_divergence":
            raise ValueError(
                "post-event summaries require a triggered divergence gate"
            )
        return self


class HistoricalCardNarrative(_Frozen):
    """Analyst-authored readable text, bounded to cited descriptive facts."""

    executive_summary: NonEmptyStr
    key_messages: tuple[NonEmptyStr, ...] = Field(min_length=1)
    metric_interpretation: NonEmptyStr
    reaction_interpretation: NonEmptyStr
    caveats: tuple[NonEmptyStr, ...] = Field(min_length=1)


class ManagementGuidanceAvailability(_Frozen):
    """Whether cutoff-native management guidance is usable by this artifact."""

    status: Literal["available", "unavailable", "retrieval_failed"]
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def reason_matches_status(self) -> ManagementGuidanceAvailability:
        if self.status == "available" and self.reason is not None:
            raise ValueError(
                "available management guidance cannot carry a missing reason"
            )
        if self.status != "available" and self.reason is None:
            raise ValueError("unavailable management guidance requires a reason")
        return self


def _default_guidance_availability() -> ManagementGuidanceAvailability:
    return ManagementGuidanceAvailability(
        status="unavailable",
        reason=(
            "No cutoff-native management-guidance evidence was supplied to the "
            "historical-card builder; issuer-level absence was not validated."
        ),
    )


class HistoricalCardBuildInput(_Frozen):
    card_id: NonEmptyStr
    event_id: NonEmptyStr
    ticker: NonEmptyStr
    company: NonEmptyStr
    fiscal_period: FiscalPeriod
    event_at: datetime
    historical_event_cutoff_at: datetime
    target_cutoff_at: datetime
    generated_at: datetime
    actual_metrics: tuple[MetricValue, ...] = Field(min_length=1)
    issuer_source_url: NonEmptyStr
    issuer_source_claim: NonEmptyStr
    issuer_source_sha256: Sha256
    issuer_source_retrieved_at: datetime
    market_source_sha256: Sha256
    benchmark_ticker: NonEmptyStr
    benchmark_source_sha256: Sha256
    market_after_hours_source_sha256: Sha256 | None = None
    benchmark_after_hours_source_sha256: Sha256 | None = None
    pre_event_features: PreEventFeatureSnapshot
    native_news_summary: NativeNewsLabelSummary | None = None
    news_evidence_binding: HistoricalNewsEvidenceBinding | None = None
    reconstructed_eps_context: ReconstructedEpsContext | None = None
    pre_event_missing_values: tuple[MissingValue, ...] = ()
    prior_20d_average_volume: Decimal | None = Field(default=None, ge=0)
    reference_session_volume_vs_prior_20d: Decimal | None = Field(default=None, ge=0)
    reaction: MarketReaction
    narrative: HistoricalCardNarrative
    management_guidance_availability: ManagementGuidanceAvailability = Field(
        default_factory=_default_guidance_availability
    )

    @model_validator(mode="after")
    def identities_and_cutoffs_match(self) -> HistoricalCardBuildInput:
        timestamps = (
            self.event_at,
            self.historical_event_cutoff_at,
            self.target_cutoff_at,
            self.generated_at,
            self.issuer_source_retrieved_at,
        )
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in timestamps
        ):
            raise ValueError("historical-card timestamps must be timezone-aware")
        if self.ticker.upper() != self.pre_event_features.ticker:
            raise ValueError("pre-event features must match the card ticker")
        if self.event_id != self.pre_event_features.event_id:
            raise ValueError("pre-event features must match the card event")
        if self.event_at.date() != self.pre_event_features.event_date:
            raise ValueError("pre-event features must match the card event date")
        if self.pre_event_features.cutoff_at != self.historical_event_cutoff_at:
            raise ValueError(
                "pre-event feature cutoff must match the historical cutoff"
            )
        if self.reaction.fiscal_period != self.fiscal_period.label:
            raise ValueError("reaction must match the card fiscal period")
        if self.generated_at >= self.target_cutoff_at:
            raise ValueError(
                "card generation must be strictly before the target cutoff"
            )
        if self.issuer_source_retrieved_at > self.generated_at:
            raise ValueError("issuer source retrieval cannot follow card generation")
        if self.issuer_source_retrieved_at >= self.target_cutoff_at:
            raise ValueError("issuer source retrieval must precede the target cutoff")
        for retrieved_at in (
            self.native_news_summary.retrieved_at if self.native_news_summary else None,
            self.reconstructed_eps_context.retrieved_at
            if self.reconstructed_eps_context
            else None,
        ):
            if retrieved_at is not None and retrieved_at > self.generated_at:
                raise ValueError(
                    "supplemental evidence retrieval cannot follow generation"
                )
        if (
            self.reaction.returns.after_hours_pct is not None
            and self.market_after_hours_source_sha256 is None
        ):
            raise ValueError("after-hours return requires a market source checksum")
        adjusted = self.reaction.benchmark_adjusted_returns
        if (
            adjusted is not None
            and adjusted.after_hours_pct is not None
            and self.benchmark_after_hours_source_sha256 is None
        ):
            raise ValueError(
                "benchmark-adjusted after-hours return requires a benchmark checksum"
            )
        metric_keys = tuple(metric.metric for metric in self.actual_metrics)
        if len(set(metric_keys)) != len(metric_keys):
            raise ValueError("actual metrics must be unique by normalized metric")
        if any(
            metric.fiscal_period != self.fiscal_period for metric in self.actual_metrics
        ):
            raise ValueError("actual metrics must match the card fiscal period")
        return self


class HistoricalContextCardArtifact(_Frozen):
    """Canonical v4 card plus the readable projection required by EL-005."""

    schema_version: Literal[HISTORICAL_CARD_ARTIFACT_VERSION] = (
        HISTORICAL_CARD_ARTIFACT_VERSION
    )
    card_id: NonEmptyStr
    ticker: NonEmptyStr
    event_id: NonEmptyStr
    event_date: date
    knowledge_cutoff_at: datetime
    available_at: datetime
    target_cutoff_at: datetime
    status: Literal["reviewed", "approved"]
    executive_summary: NonEmptyStr
    key_messages: tuple[NonEmptyStr, ...] = Field(min_length=1)
    metric_interpretation: NonEmptyStr
    reaction_interpretation: NonEmptyStr
    pre_event_features: dict[str, object]
    reaction_metrics: dict[str, object]
    native_news_summary: NativeNewsLabelSummary | None = None
    news_evidence_binding: HistoricalNewsEvidenceBinding | None = None
    reconstructed_eps_context: ReconstructedEpsContext | None = None
    management_guidance_availability: ManagementGuidanceAvailability | None = None
    data_quality_caveats: tuple[NonEmptyStr, ...] = Field(min_length=1)
    provenance_links: tuple[NonEmptyStr, ...] = Field(min_length=1)
    citation_uri: NonEmptyStr
    content_sha256: Sha256
    canonical_card: ContextCardV4
    approved_at: datetime | None = None
    approved_by: NonEmptyStr | None = None

    @model_validator(mode="after")
    def artifact_matches_canonical_card(self) -> HistoricalContextCardArtifact:
        card = ContextCardV4.model_validate(self.canonical_card.model_dump(mode="json"))
        if self.content_sha256 != sha256_hex(card.model_dump(mode="json")):
            raise ValueError("historical-card checksum must match the canonical card")
        if (
            self.ticker != card.identity.ticker
            or self.status != card.identity.card.status
        ):
            raise ValueError("artifact identity/status must match the canonical card")
        if self.knowledge_cutoff_at != card.identity.card.as_of:
            raise ValueError(
                "artifact knowledge cutoff must match canonical card as_of"
            )
        event_at = card.identity.event.event_datetime
        if event_at is None or self.event_date != event_at.date():
            raise ValueError("artifact event date must match canonical card event")
        if self.available_at >= self.target_cutoff_at:
            raise ValueError(
                "historical card must be available before the target cutoff"
            )
        if self.available_at < self.knowledge_cutoff_at:
            raise ValueError(
                "historical card cannot be available before its knowledge cutoff"
            )
        if self.citation_uri != f"eventlens-context-card:{self.card_id}":
            raise ValueError("historical-card citation URI must bind its card ID")
        guidance = self.management_guidance_availability
        ledger_populated = bool(card.management_guidance_ledger)
        if guidance is not None and (
            (guidance.status == "available") != ledger_populated
        ):
            raise ValueError(
                "management-guidance availability must match the canonical ledger"
            )
        approved = self.status == "approved"
        if approved != (self.approved_at is not None and self.approved_by is not None):
            raise ValueError(
                "only an approved artifact carries approval time and actor"
            )
        if approved and self.approved_at != self.available_at:
            raise ValueError(
                "approved artifact availability must equal its approval time"
            )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json")) + b"\n"

    def as_prior_evidence(self) -> PriorContextCardEvidence:
        """Return synthesis input only after explicit owner approval."""

        if self.status != "approved":
            raise ValueError(
                "prior context card must be explicitly approved before attachment"
            )
        if (
            self.news_evidence_binding is not None
            and self.news_evidence_binding.reconstructed_at >= self.target_cutoff_at
        ):
            raise ValueError(
                "retrospective news reconstruction cannot attach to a live bundle"
            )
        return self._prior_evidence()

    def as_replay_prior_evidence(self) -> PriorContextCardEvidence:
        """Return explicitly retrospective input for a replay-prefixed bundle."""

        if self.status != "approved" or self.news_evidence_binding is None:
            raise ValueError("replay attachment requires an approved enriched card")
        return self._prior_evidence()

    def _prior_evidence(self) -> PriorContextCardEvidence:
        return PriorContextCardEvidence(
            prior_event_id=self.event_id,
            card_id=self.card_id,
            event_date=self.event_date,
            knowledge_cutoff_at=self.knowledge_cutoff_at,
            card=self.canonical_card,
            card_sha256=self.content_sha256,
            receipt=ProductEvidenceReceipt(
                component="prior_context_card",
                receipt_id=f"prior-context-card:{self.card_id}",
                content_sha256=self.content_sha256,
                available_at=self.available_at,
                source_ref=self.citation_uri,
                provenance_refs=tuple(sorted(self.provenance_links)),
            ),
        )


def _evidence_receipts(value: HistoricalCardBuildInput) -> tuple[EvidenceReceipt, ...]:
    ticker = value.ticker.upper()
    market = f"market-history:{ticker}:{value.event_id}"
    benchmark = f"market-history:{value.benchmark_ticker.upper()}:{value.event_id}"
    receipts = [
        EvidenceReceipt(
            id=f"issuer-results:{value.event_id}",
            evidence_class=EvidenceClass.SOURCE_NATIVE,
            claim=value.issuer_source_claim,
            source_ref=(
                f"{value.issuer_source_url}"
                f"#fact-manifest-sha256={value.issuer_source_sha256}"
            ),
        ),
        EvidenceReceipt(
            id=market,
            evidence_class=EvidenceClass.SOURCE_NATIVE,
            claim=f"Normalized {ticker} OHLCV observations used for market metrics.",
            source_ref=f"yfinance:{ticker}#sha256={value.market_source_sha256}",
        ),
        EvidenceReceipt(
            id=benchmark,
            evidence_class=EvidenceClass.SOURCE_NATIVE,
            claim=f"Normalized {value.benchmark_ticker.upper()} OHLCV benchmark observations.",
            source_ref=(
                f"yfinance:{value.benchmark_ticker.upper()}"
                f"#sha256={value.benchmark_source_sha256}"
            ),
        ),
        EvidenceReceipt(
            id=f"market-reaction:{value.event_id}",
            evidence_class=EvidenceClass.DERIVED,
            claim="Deterministic factual reaction windows; no causal attribution was made.",
            input_receipt_ids=tuple(
                filter(
                    None,
                    (
                        market,
                        benchmark,
                        f"massive-after-hours:{ticker}:{value.event_id}"
                        if value.market_after_hours_source_sha256
                        else None,
                        f"massive-after-hours:{value.benchmark_ticker.upper()}:{value.event_id}"
                        if value.benchmark_after_hours_source_sha256
                        else None,
                    ),
                )
            ),
            operands_validated=True,
        ),
        EvidenceReceipt(
            id=f"historical-consensus-unavailable:{value.event_id}",
            evidence_class=EvidenceClass.RECONSTRUCTED,
            claim="No cutoff-native historical sell-side estimate was available to this run.",
            source_ref="eventlens-missingness:historical-consensus-not-collected",
        ),
    ]
    if value.market_after_hours_source_sha256 is not None:
        receipts.insert(
            3,
            EvidenceReceipt(
                id=f"massive-after-hours:{ticker}:{value.event_id}",
                evidence_class=EvidenceClass.SOURCE_NATIVE,
                claim=f"Massive unadjusted {ticker} daily summary supplied the after-hours close.",
                source_ref=(
                    f"massive-open-close:{ticker}:{value.event_at.date()}"
                    f"#sha256={value.market_after_hours_source_sha256}"
                ),
            ),
        )
    if value.benchmark_after_hours_source_sha256 is not None:
        receipts.insert(
            4,
            EvidenceReceipt(
                id=f"massive-after-hours:{value.benchmark_ticker.upper()}:{value.event_id}",
                evidence_class=EvidenceClass.SOURCE_NATIVE,
                claim=(
                    f"Massive unadjusted {value.benchmark_ticker.upper()} daily summary "
                    "supplied the benchmark after-hours close."
                ),
                source_ref=(
                    f"massive-open-close:{value.benchmark_ticker.upper()}:{value.event_at.date()}"
                    f"#sha256={value.benchmark_after_hours_source_sha256}"
                ),
            ),
        )
    if value.native_news_summary is not None:
        news = value.native_news_summary
        receipts.append(
            EvidenceReceipt(
                id=f"massive-news:{value.event_id}",
                evidence_class=EvidenceClass.RECONSTRUCTED,
                claim=(
                    f"Massive returned {news.query_article_count} pre-cutoff ticker-news rows; "
                    f"the deterministic headline issuer-name rule retained "
                    f"{news.issuer_named_article_count}."
                ),
                source_ref=(
                    f"massive-news:{ticker}:{value.historical_event_cutoff_at.isoformat()}"
                    f"#sha256={news.raw_response_sha256}"
                ),
            )
        )
    if value.news_evidence_binding is not None:
        receipts.append(
            _historical_news_binding_receipt(
                value.event_id, value.news_evidence_binding
            )
        )
    if value.reconstructed_eps_context is not None:
        eps = value.reconstructed_eps_context
        receipts.append(
            EvidenceReceipt(
                id=f"reconstructed-eps:{value.event_id}",
                evidence_class=EvidenceClass.RECONSTRUCTED,
                claim=(
                    "Alpha Vantage historical estimated/reported EPS was retrieved "
                    "retrospectively and is excluded from surprise computation."
                ),
                source_ref=(
                    f"alpha-vantage-earnings:{ticker}#sha256={eps.raw_response_sha256}"
                ),
            )
        )
    return tuple(receipts)


def build_historical_context_card(
    value: HistoricalCardBuildInput,
) -> HistoricalContextCardArtifact:
    """Build and deterministically QC one readable historical card candidate."""

    ticker = value.ticker.upper()
    receipts = _evidence_receipts(value)
    receipt_ids = {receipt.id for receipt in receipts}
    issuer_receipt = f"issuer-results:{value.event_id}"
    reaction_receipt = f"market-reaction:{value.event_id}"
    consensus_receipt = f"historical-consensus-unavailable:{value.event_id}"
    metrics = {metric.metric: metric for metric in value.actual_metrics}
    quarter = HistoricalQuarter(
        fiscal_period=value.fiscal_period,
        expectations=(
            Expectation(
                consensus_type=ConsensusType.SELL_SIDE,
                fiscal_period=value.fiscal_period,
                metrics={},
                evidence_refs=(consensus_receipt,),
                availability_status="unknown",
            ),
        ),
        actual_results=ActualResults(
            fiscal_period=value.fiscal_period,
            metrics=metrics,
            evidence_refs=(issuer_receipt,),
        ),
        surprise_layers=SurpriseLayers(),
        reaction=value.reaction,
        evidence_refs=(issuer_receipt, reaction_receipt),
    )
    payload = {
        "identity": {
            "ticker": ticker,
            "company": value.company,
            "event": {
                "type": "earnings",
                "fiscal_period": value.fiscal_period.label,
                "event_datetime": value.event_at,
                "timezone": "America/New_York",
                "event_datetime_status": "confirmed",
            },
            "card": {
                "version": 4,
                "generated_at": value.generated_at,
                "as_of": value.generated_at,
                "sha256": None,
                "status": "draft",
            },
        },
        "temporal_state": {
            "historical_event_cutoff": value.historical_event_cutoff_at,
            "observations": {},
        },
        "decision_context": {"currency": "USD"},
        "current_pre_event_state": {},
        "company_ontology": {
            "ontology_id": "universal",
            "version": "1.0.0",
            "reference": "package://eventlens.ontologies.vintages/universal/1.0.0.yaml",
        },
        "historical_quarters": (quarter,),
        "management_guidance_ledger": (),
        "consensus_state": {
            "status": "missing",
            "fiscal_period": value.fiscal_period.label,
            "blockers": (
                "Historical cutoff-native sell-side consensus was not collected.",
            ),
        },
        "market_reaction_history": (value.reaction,),
        "thesis_state": {},
        "scenario_model": {},
        "decision": {},
        "evidence_registry": {"receipts": receipts, "semantic_claims": ()},
        "conflicts": (),
        "quality_control": {
            "publication_status": "draft",
            "warnings": value.narrative.caveats,
            "requires_human_review": True,
        },
        "provenance": {
            "ontology_version": "1.0.0",
            "retrieval_version": "historical-context-card-v1",
            "normalization_version": "context-card-v4",
            "reaction_version": REACTION_METHODOLOGY_VERSION,
            "source_manifest_sha256": sha256_hex(
                {
                    "issuer": value.issuer_source_sha256,
                    "market": value.market_source_sha256,
                    "benchmark": value.benchmark_source_sha256,
                    "market_after_hours": value.market_after_hours_source_sha256,
                    "benchmark_after_hours": value.benchmark_after_hours_source_sha256,
                    "news": (
                        value.native_news_summary.raw_response_sha256
                        if value.native_news_summary
                        else None
                    ),
                    "private_news_manifest": (
                        value.news_evidence_binding.private_manifest_sha256
                        if value.news_evidence_binding
                        else None
                    ),
                    "public_news_projection": (
                        value.news_evidence_binding.public_projection_sha256
                        if value.news_evidence_binding
                        else None
                    ),
                    "eps": (
                        value.reconstructed_eps_context.raw_response_sha256
                        if value.reconstructed_eps_context
                        else None
                    ),
                }
            ),
            "generated_at": value.generated_at,
        },
    }
    draft = ContextCardV4.model_validate(payload)
    uses = {
        receipt_id: (AnalyticalUse.DESCRIPTIVE_CONTEXT,) for receipt_id in receipt_ids
    }
    report = validate_card(draft, analytical_uses=uses)
    if report.publication_status is not PublicationStatus.REVIEWED or report.blockers:
        raise ValueError("historical context card failed deterministic review")
    reviewed = draft.model_copy(
        update={
            "identity": draft.identity.model_copy(
                update={
                    "card": draft.identity.card.model_copy(
                        update={"status": "reviewed"}
                    ),
                }
            ),
            "quality_control": draft.quality_control.model_copy(
                update={
                    "publication_status": "reviewed",
                    "blockers": (),
                    "warnings": tuple(
                        dict.fromkeys(
                            (
                                *value.narrative.caveats,
                                *(issue.message for issue in report.warnings),
                            )
                        )
                    ),
                    "requires_human_review": True,
                }
            ),
        }
    )
    feature = value.pre_event_features.model_dump(mode="json")
    feature.update(
        {
            "prior_20d_average_volume": value.prior_20d_average_volume,
            "missing_values": [
                item.model_dump(mode="json") for item in value.pre_event_missing_values
            ],
        }
    )
    for item in value.pre_event_missing_values:
        if item.field in feature:
            feature[item.field] = None
    reaction = value.reaction.returns
    reaction_missing = (
        ()
        if reaction.after_hours_pct is not None
        else (
            MissingValue(
                field="after_hours_return_pct",
                reason="No retained after-hours quote was available; no value was inferred.",
            ),
        )
    )
    content_sha = sha256_hex(reviewed.model_dump(mode="json"))
    return HistoricalContextCardArtifact(
        card_id=value.card_id,
        ticker=ticker,
        event_id=value.event_id,
        event_date=value.event_at.date(),
        knowledge_cutoff_at=value.generated_at,
        available_at=value.generated_at,
        target_cutoff_at=value.target_cutoff_at,
        status="reviewed",
        executive_summary=value.narrative.executive_summary,
        key_messages=value.narrative.key_messages,
        metric_interpretation=value.narrative.metric_interpretation,
        reaction_interpretation=value.narrative.reaction_interpretation,
        pre_event_features=feature,
        reaction_metrics={
            "after_hours_return_pct": reaction.after_hours_pct,
            "next_session_open_vs_reference_close_pct": reaction.next_session_open_vs_reference_close_pct,
            "t_plus_1_close_return_pct": reaction.t_plus_1_pct,
            "t_plus_5_close_return_pct": reaction.t_plus_5_pct,
            "absolute_next_session_open_move_pct": (
                abs(reaction.next_session_open_vs_reference_close_pct)
                if reaction.next_session_open_vs_reference_close_pct is not None
                else None
            ),
            "reference_session_volume_vs_prior_20d": (
                value.reference_session_volume_vs_prior_20d
            ),
            "missing_values": [
                item.model_dump(mode="json") for item in reaction_missing
            ],
        },
        native_news_summary=value.native_news_summary,
        news_evidence_binding=value.news_evidence_binding,
        reconstructed_eps_context=value.reconstructed_eps_context,
        management_guidance_availability=value.management_guidance_availability,
        data_quality_caveats=value.narrative.caveats,
        provenance_links=tuple(
            filter(
                None,
                (
                    value.issuer_source_url,
                    f"yfinance:{ticker}#sha256={value.market_source_sha256}",
                    f"yfinance:{value.benchmark_ticker.upper()}#sha256={value.benchmark_source_sha256}",
                    (
                        f"massive-open-close:{ticker}:{value.event_at.date()}"
                        f"#sha256={value.market_after_hours_source_sha256}"
                    )
                    if value.market_after_hours_source_sha256
                    else None,
                    (
                        f"massive-open-close:{value.benchmark_ticker.upper()}:{value.event_at.date()}"
                        f"#sha256={value.benchmark_after_hours_source_sha256}"
                    )
                    if value.benchmark_after_hours_source_sha256
                    else None,
                    (
                        f"massive-news:{ticker}:{value.historical_event_cutoff_at.isoformat()}"
                        f"#sha256={value.native_news_summary.raw_response_sha256}"
                    )
                    if value.native_news_summary
                    else None,
                    (
                        f"eventlens-private-news-evidence:"
                        f"{value.news_evidence_binding.private_manifest_sha256}"
                    )
                    if value.news_evidence_binding
                    else None,
                    (
                        f"eventlens-public-news-evidence:"
                        f"{value.news_evidence_binding.public_projection_sha256}"
                    )
                    if value.news_evidence_binding
                    else None,
                    (
                        f"alpha-vantage-earnings:{ticker}"
                        f"#sha256={value.reconstructed_eps_context.raw_response_sha256}"
                    )
                    if value.reconstructed_eps_context
                    else None,
                ),
            )
        ),
        citation_uri=f"eventlens-context-card:{value.card_id}",
        content_sha256=content_sha,
        canonical_card=reviewed,
    )


def _historical_news_binding_receipt(
    event_id: str, binding: HistoricalNewsEvidenceBinding
) -> EvidenceReceipt:
    return EvidenceReceipt(
        id=f"historical-news-evidence:{event_id}",
        evidence_class=EvidenceClass.RECONSTRUCTED,
        claim=(
            f"Historical news resolver retained {binding.selected_passage_count} "
            f"passage(s) and {len(binding.selected_provider_summaries)} selected "
            f"provider-summary reference(s), with "
            f"{len(binding.provider_summary_pulls)} private direct pull(s), across "
            f"{binding.item_count} discovered item(s); semantic status is "
            f"{binding.semantic_status}."
        ),
        source_ref=(
            f"eventlens-private-news-evidence:{binding.private_manifest_sha256}"
        ),
    )


__all__ = [
    "HISTORICAL_CARD_ARTIFACT_VERSION",
    "HistoricalCardBuildInput",
    "HistoricalCardNarrative",
    "HistoricalContextCardArtifact",
    "HistoricalNewsEvidenceBinding",
    "HistoricalProviderSummaryPull",
    "MissingValue",
    "NativeNewsLabelSummary",
    "PostEventExplanationGate",
    "ReconstructedEpsContext",
    "build_historical_context_card",
    "build_historical_provider_summary_pulls",
    "evaluate_post_event_explanation_gate",
]
