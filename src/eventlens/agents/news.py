"""Typed, cutoff-safe contract for per-item news semantic classification.

No model transport lives here.  A caller supplies a validated result from a
model/agent; this module verifies that it says nothing beyond the retained,
selected passages and the immutable discovery/content receipts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.artifacts.models import sha256_hex
from eventlens.providers.content import ContentEvidence, ContentFetchStatus, bound_excerpt
from eventlens.providers.models import NewsArticleMetadata, NewsSummaryStatus
from eventlens.providers.passage_selection import (
    DuplicateCluster,
    DuplicateClustering,
    PassageSelection,
    article_candidate_from_content,
)

from .models import AgentSkill


NEWS_CLASSIFICATION_SCHEMA_VERSION = "eventlens.news-classification.v1"
CONTENT_CLAIM_EXTRACTOR_VERSION = "1.0"
NEWS_MATERIALITY_AND_SENTIMENT_VERSION = "1.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class NewsEntityMatch(StrEnum):
    EXACT = "exact"
    RELATED = "related"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class NewsEventRelationship(StrEnum):
    PRE_EARNINGS = "pre_earnings"
    EARNINGS_RELATED = "earnings_related"
    POST_EARNINGS = "post_earnings"
    COMPANY_FUNDAMENTAL = "company_fundamental"
    UNRELATED = "unrelated"
    UNKNOWN = "unknown"


class NewsTopic(StrEnum):
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    REVENUE_DEMAND = "revenue_demand"
    PROFITABILITY = "profitability"
    CAPITAL_ALLOCATION = "capital_allocation"
    MANAGEMENT = "management"
    CONSENSUS = "consensus"
    REGULATORY = "regulatory"
    COMPETITION = "competition"
    RISK = "risk"
    OTHER = "other"


class NewsRelevance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"
    UNKNOWN = "unknown"


class NewsMateriality(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"
    UNKNOWN = "unknown"


class NewsTone(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class NewsImplication(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class NewsHorizon(StrEnum):
    IMMEDIATE = "immediate"
    NEXT_EARNINGS = "next_earnings"
    MEDIUM_TERM = "medium_term"
    LONG_TERM = "long_term"
    UNKNOWN = "unknown"


class NewsNovelty(StrEnum):
    NOVEL = "novel"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"


class NewsSkillDescriptor(_Frozen):
    skill_id: Literal["content_claim_extractor", "news_materiality_and_sentiment"]
    version: str = Field(min_length=1)
    allowed_read_only_tools: tuple[str, ...] = ()
    forbidden_behaviors: tuple[str, ...] = Field(min_length=1)
    abstention_conditions: tuple[str, ...] = Field(min_length=1)
    acceptance_checks: tuple[str, ...] = Field(min_length=1)


NEWS_SKILL_DESCRIPTORS = (
    NewsSkillDescriptor(
        skill_id="content_claim_extractor",
        version=CONTENT_CLAIM_EXTRACTOR_VERSION,
        allowed_read_only_tools=("selected_passage_reader", "provider_summary_reader"),
        forbidden_behaviors=("do_not_cite_unselected_or_unretained_text", "do_not_invent_facts"),
        abstention_conditions=("headline_only", "unretained_or_insufficient_summary"),
        acceptance_checks=(
            "every_claim_cites_a_selected_passage_or_permitted_summary",
            "claim_text_is_bounded",
        ),
    ),
    NewsSkillDescriptor(
        skill_id="news_materiality_and_sentiment",
        version=NEWS_MATERIALITY_AND_SENTIMENT_VERSION,
        allowed_read_only_tools=("selected_passage_reader", "provider_summary_reader", "ontology_slot_reader"),
        forbidden_behaviors=("do_not_conflate_provider_native_and_agent_sentiment", "do_not_access_unretained_body"),
        abstention_conditions=("headline_only", "unretained_or_insufficient_summary"),
        acceptance_checks=(
            "content_fields_require_selected_passage_or_permitted_summary_citation",
            "ontology_slots_are_allowlisted",
        ),
    ),
)

NEWS_AGENT_SKILLS = tuple(
    AgentSkill(skill_id=item.skill_id, description=(
        "Extract cited factual claims only from selected passages or a permitted provider summary."
        if item.skill_id == "content_claim_extractor"
        else "Classify cited passage or permitted-summary content while keeping headline, content, and provider-native sentiment distinct."
    ))
    for item in NEWS_SKILL_DESCRIPTORS
)


class NewsProviderNativeSentiment(_Frozen):
    """Metadata copied verbatim from discovery; never agent-generated sentiment."""

    overall_score: Decimal | None = None
    overall_label: str | None = None
    ticker_relevance_score: Decimal | None = None
    ticker_sentiment_score: Decimal | None = None
    ticker_sentiment_label: str | None = None

    @classmethod
    def from_article(cls, article: NewsArticleMetadata) -> "NewsProviderNativeSentiment":
        return cls(
            overall_score=article.overall_sentiment_score,
            overall_label=article.overall_sentiment_label,
            ticker_relevance_score=article.ticker_relevance_score,
            ticker_sentiment_score=article.ticker_sentiment_score,
            ticker_sentiment_label=article.ticker_sentiment_label,
        )


class NewsPassageCitation(_Frozen):
    passage_id: str = Field(min_length=1)


class NewsSummaryCitation(_Frozen):
    """Checksum pointer to a provider summary, never a body-passage ID."""

    summary_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class NewsClassifiedClaim(_Frozen):
    claim_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,80}$")
    text: str = Field(min_length=1, max_length=1_000)
    citations: tuple[NewsPassageCitation, ...] = Field(default=(), max_length=4)
    summary_citations: tuple[NewsSummaryCitation, ...] = Field(default=(), max_length=1)
    confidence: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def citations_are_unique(self) -> "NewsClassifiedClaim":
        ids = tuple(item.passage_id for item in self.citations)
        if len(set(ids)) != len(ids):
            raise ValueError("classified claim citations must be unique")
        if not (self.citations or self.summary_citations):
            raise ValueError("classified claim requires a passage or summary citation")
        return self


class NewsUsageReceipt(_Frozen):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @field_validator("started_at", "completed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("usage timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ordered(self) -> "NewsUsageReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("usage completed_at must not precede started_at")
        return self


class NewsClassificationRequest(_Frozen):
    schema_version: Literal[NEWS_CLASSIFICATION_SCHEMA_VERSION] = NEWS_CLASSIFICATION_SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1, max_length=16)
    cutoff_at: datetime
    article: NewsArticleMetadata
    content_evidence: ContentEvidence
    passage_selection: PassageSelection
    duplicate_clustering: DuplicateClustering | None = None
    allowed_ontology_slot_ids: tuple[str, ...] = ()
    skill_descriptors: tuple[NewsSkillDescriptor, ...] = NEWS_SKILL_DESCRIPTORS
    prompt_version: str = Field(min_length=1)
    request_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("ticker")
    @classmethod
    def ticker_upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("cutoff_at")
    @classmethod
    def cutoff_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cutoff_at must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError("cutoff_at must use UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def receipt_chain_is_cutoff_safe(self) -> "NewsClassificationRequest":
        if self.article.ticker != self.ticker:
            raise ValueError("article ticker must match classification ticker")
        if self.article.published_at >= self.cutoff_at:
            raise ValueError("article must be strictly before classification cutoff")
        evidence = self.content_evidence
        if evidence.provider_article_id != self.article.provider_article_id:
            raise ValueError("content evidence article ID does not match metadata")
        if evidence.article_metadata_sha256 != self.article.item_sha256:
            raise ValueError("content evidence article metadata checksum does not match")
        if evidence.discovery_sha256 != self.article.discovery_fingerprint:
            raise ValueError("content evidence discovery fingerprint does not match")
        if evidence.cutoff_at != self.cutoff_at:
            raise ValueError("content evidence cutoff does not match classification cutoff")
        if evidence.evidence_sha256 != evidence.content_checksum():
            raise ValueError("content evidence checksum does not match")
        selection = self.passage_selection
        if selection.article_id != self.article.provider_article_id:
            raise ValueError("passage selection article ID does not match metadata")
        if selection.article_metadata_sha256 != self.article.item_sha256:
            raise ValueError("passage selection metadata checksum does not match")
        if selection.article_discovery_fingerprint != self.article.discovery_fingerprint:
            raise ValueError("passage selection discovery fingerprint does not match")
        if selection.evidence_sha256 != evidence.evidence_sha256:
            raise ValueError("passage selection evidence checksum does not match")
        self._validate_selection_against_retained_content()
        if self.duplicate_clustering is not None:
            # The complete clustering receipt is carried, rather than a
            # detached cluster fragment.  Reconstruct the canonical candidate
            # from the same rights-safe evidence and compare every field, so a
            # rechecksummed clustering cannot substitute body text, omit its
            # source URL, or claim a different retention state for this item.
            clustering_inputs = tuple(
                item for item in self.duplicate_clustering.input_articles
                if item.article_id == self.article.provider_article_id
            )
            if len(clustering_inputs) != 1:
                raise ValueError("duplicate clustering must contain exactly one input for article")
            expected_candidate = article_candidate_from_content(self.article, evidence)
            if clustering_inputs[0] != expected_candidate:
                raise ValueError("duplicate clustering input does not exactly match content-derived article candidate")
        if self.duplicate_cluster is not None:
            if self.article.provider_article_id not in self.duplicate_cluster.article_ids:
                raise ValueError("derived duplicate cluster does not contain article")
            if self.article.discovery_fingerprint not in self.duplicate_cluster.member_discovery_fingerprints:
                raise ValueError("derived duplicate cluster does not contain article fingerprint")
        if len(set(self.allowed_ontology_slot_ids)) != len(self.allowed_ontology_slot_ids):
            raise ValueError("allowed ontology slot IDs must be unique")
        if self.skill_descriptors != NEWS_SKILL_DESCRIPTORS:
            raise ValueError("classification request must use the supported news skill descriptors")
        expected = self.content_checksum()
        if self.request_sha256 is None:
            object.__setattr__(self, "request_sha256", expected)
        elif self.request_sha256 != expected:
            raise ValueError("request_sha256 does not match classification request")
        return self

    def content_checksum(self) -> str:
        return sha256_hex(self.model_dump(mode="json", exclude={"request_sha256"}))

    @property
    def duplicate_cluster(self) -> DuplicateCluster | None:
        if self.duplicate_clustering is None:
            return None
        matches = tuple(
            cluster for cluster in self.duplicate_clustering.clusters
            if self.article.provider_article_id in cluster.article_ids
        )
        if len(matches) > 1:  # defensive; DuplicateClustering already rejects this.
            raise ValueError("article belongs to multiple duplicate clusters")
        return matches[0] if matches else None

    @property
    def provider_summary_sha256(self) -> str | None:
        if self.article.summary_status is not NewsSummaryStatus.RETAINED_PERMITTED:
            return None
        if self.article.provider_summary is None:
            return None
        return sha256_hex({"article_metadata_sha256": self.article.item_sha256, "summary": self.article.provider_summary})

    def _validate_selection_against_retained_content(self) -> None:
        evidence = self.content_evidence
        selection = self.passage_selection
        if selection.content_policy_sha256 != evidence.policy_sha256:
            raise ValueError("passage selection content policy checksum does not match evidence")
        if selection.grant_id != evidence.grant_id or selection.grant_version != evidence.grant_version:
            raise ValueError("passage selection grant does not match evidence")
        if selection.evidence_sha256 != evidence.evidence_sha256:
            raise ValueError("passage selection evidence checksum does not match")
        if selection.policy.max_excerpt_characters != evidence.excerpt_character_limit:
            raise ValueError("passage selection excerpt bound does not match evidence")
        paragraphs = {item.paragraph_id: item for item in evidence.paragraphs}
        retained = {item.paragraph_id: item for item in evidence.excerpts}
        for item in selection.selected_passages:
            paragraph = paragraphs.get(item.paragraph_id)
            if paragraph is None or paragraph.text_sha256 != item.paragraph_sha256:
                raise ValueError("selected passage ID/hash does not match content evidence")
            source_text = paragraph.text
            if source_text is None:
                excerpt = retained.get(item.paragraph_id)
                source_text = excerpt.text if excerpt is not None else None
            if source_text is None or item.excerpt != bound_excerpt(source_text, selection.policy.max_excerpt_characters):
                raise ValueError("selected passage excerpt does not exactly match retained content")


class NewsClassificationResult(_Frozen):
    schema_version: Literal[NEWS_CLASSIFICATION_SCHEMA_VERSION] = NEWS_CLASSIFICATION_SCHEMA_VERSION
    request: NewsClassificationRequest
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    entity_match: NewsEntityMatch
    event_relationship: NewsEventRelationship
    topics: tuple[NewsTopic, ...] = ()
    ontology_slot_ids: tuple[str, ...] = ()
    relevance: NewsRelevance
    materiality: NewsMateriality
    cited_claims: tuple[NewsClassifiedClaim, ...] = ()
    agent_headline_tone: NewsTone
    agent_content_tone: NewsTone | None = None
    agent_fundamental_implication: NewsImplication | None = None
    agent_near_term_market_implication: NewsImplication | None = None
    horizon: NewsHorizon | None = None
    novelty: NewsNovelty
    confidence: Decimal = Field(ge=0, le=1)
    abstention_reason: str | None = Field(default=None, max_length=1_000)
    provider_native_sentiment: NewsProviderNativeSentiment
    skill_id: Literal["news_materiality_and_sentiment"] = "news_materiality_and_sentiment"
    skill_version: Literal[NEWS_MATERIALITY_AND_SENTIMENT_VERSION] = NEWS_MATERIALITY_AND_SENTIMENT_VERSION
    prompt_version: str = Field(min_length=1)
    usage_receipt: NewsUsageReceipt
    result_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_result_against_immutable_input(self) -> "NewsClassificationResult":
        request = self.request
        if self.request_sha256 != request.request_sha256:
            raise ValueError("result request_sha256 does not match request")
        if self.prompt_version != request.prompt_version:
            raise ValueError("result prompt_version does not match request")
        if self.usage_receipt.prompt_version != self.prompt_version:
            raise ValueError("usage receipt prompt_version does not match result")
        allowed_slots = set(request.allowed_ontology_slot_ids)
        if not set(self.ontology_slot_ids).issubset(allowed_slots):
            raise ValueError("result ontology slots are not allowed by request")
        if len(set(self.topics)) != len(self.topics) or tuple(sorted(self.topics, key=lambda item: item.value)) != self.topics:
            raise ValueError("result topics must be unique and canonically ordered")
        if len(set(self.ontology_slot_ids)) != len(self.ontology_slot_ids) or tuple(sorted(self.ontology_slot_ids)) != self.ontology_slot_ids:
            raise ValueError("result ontology slots must be unique and canonically ordered")
        selected_ids = {item.paragraph_id for item in request.passage_selection.selected_passages}
        claim_ids = [claim.claim_id for claim in self.cited_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("classified claim IDs must be unique")
        if any(citation.passage_id not in selected_ids for claim in self.cited_claims for citation in claim.citations):
            raise ValueError("classified claim citation must reference a selected passage")
        summary_sha = request.provider_summary_sha256
        if any(
            citation.summary_sha256 != summary_sha
            for claim in self.cited_claims for citation in claim.summary_citations
        ):
            raise ValueError("classified summary citation must reference the permitted provider summary")
        body_available = (
            request.content_evidence.status is ContentFetchStatus.CONTENT_AVAILABLE
            and bool(selected_ids)
        )
        summary_available = (
            request.content_evidence.status is ContentFetchStatus.SUMMARY_ONLY
            and summary_sha is not None
        )
        evidence_available = body_available or summary_available
        content_fields = (
            self.agent_content_tone,
            self.agent_fundamental_implication,
            self.agent_near_term_market_implication,
            self.horizon,
        )
        if not body_available and any(claim.citations for claim in self.cited_claims):
            raise ValueError("non-body input cannot cite selected body passages")
        if not summary_available and any(claim.summary_citations for claim in self.cited_claims):
            raise ValueError("non-summary input cannot cite provider summary")
        if not evidence_available:
            if (
                self.cited_claims
                or self.topics
                or self.ontology_slot_ids
                or self.relevance is not NewsRelevance.UNKNOWN
                or self.materiality is not NewsMateriality.UNKNOWN
                or any(value is not None for value in content_fields)
            ):
                raise ValueError("headline_only input requires content fields and cited claims to abstain")
            if self.abstention_reason is None:
                raise ValueError("headline_only input requires abstention_reason")
        else:
            content_judgment = (
                self.topics
                or self.ontology_slot_ids
                or self.relevance not in {NewsRelevance.UNKNOWN, NewsRelevance.NONE}
                or self.materiality not in {NewsMateriality.UNKNOWN, NewsMateriality.NONE}
                or any(value is not None for value in content_fields)
            )
            if content_judgment and not self.cited_claims:
                raise ValueError("content tone, implications, and semantic judgments require cited claims")
            if self.relevance is not NewsRelevance.NONE and not self.cited_claims and self.abstention_reason is None:
                raise ValueError("content classification without cited claims requires abstention_reason")
        if self.entity_match is NewsEntityMatch.MISMATCH or self.relevance is NewsRelevance.NONE:
            if (
                self.relevance is not NewsRelevance.NONE
                or self.materiality is not NewsMateriality.NONE
                or self.cited_claims
                or self.topics
                or self.ontology_slot_ids
                or any(value is not None for value in content_fields)
                or self.abstention_reason is None
            ):
                raise ValueError("entity mismatch or relevance none requires fully abstained none classification")
        if self.novelty is NewsNovelty.DUPLICATE and request.duplicate_cluster is None:
            raise ValueError("duplicate novelty requires duplicate cluster")
        if request.duplicate_cluster is not None and self.novelty is not NewsNovelty.DUPLICATE:
            raise ValueError("duplicate cluster requires duplicate novelty")
        if self.provider_native_sentiment != NewsProviderNativeSentiment.from_article(request.article):
            raise ValueError("provider_native_sentiment must be copied exactly from article metadata")
        expected = self.content_checksum()
        if self.result_sha256 is None:
            object.__setattr__(self, "result_sha256", expected)
        elif self.result_sha256 != expected:
            raise ValueError("result_sha256 does not match classification result")
        return self

    def content_checksum(self) -> str:
        return sha256_hex(self.model_dump(mode="json", exclude={"result_sha256"}))


def news_classification_json_schema() -> dict[str, object]:
    """Return the versioned JSON Schema for an external classification result.

    The immutable request is embedded in the result so the schema documents
    the complete input/output evidence boundary, not just model prose.
    """

    return NewsClassificationResult.model_json_schema()


__all__ = [
    "CONTENT_CLAIM_EXTRACTOR_VERSION", "NEWS_AGENT_SKILLS", "NEWS_CLASSIFICATION_SCHEMA_VERSION",
    "NEWS_MATERIALITY_AND_SENTIMENT_VERSION", "NEWS_SKILL_DESCRIPTORS", "NewsClassificationRequest",
    "NewsClassificationResult", "NewsClassifiedClaim", "NewsEntityMatch", "NewsEventRelationship",
    "NewsHorizon", "NewsImplication", "NewsMateriality", "NewsNovelty", "NewsPassageCitation",
    "NewsProviderNativeSentiment", "NewsRelevance", "NewsSkillDescriptor", "NewsSummaryCitation", "NewsTone", "NewsTopic",
    "NewsUsageReceipt",
    "news_classification_json_schema",
]
