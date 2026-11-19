"""Private news-evidence manifest and non-reconstructive public projection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.providers.content import (
    ContentAccessStatus,
    ContentEvidence,
    ContentFetchPolicy,
    ContentFetchReason,
    ContentFetchStatus,
    ContentRetentionPolicy,
    ContentRightsStatus,
    ContentSourceKind,
    ContentTemporalStatus,
    RightsAwareContentFetcher,
    bound_excerpt,
)
from eventlens.providers.models import (
    NewsArticleMetadata,
    NewsSummaryStatus,
    NewsTickerMatch,
)
from eventlens.providers.passage_selection import (
    PassageSelection,
    PassageSelectionPolicy,
    SelectedPassage,
    passage_excerpt_selector,
    select_content_passages,
)

from .live_evidence import LiveEvidenceContract, RetainedLiveEvidence

PRIVATE_NEWS_SCHEMA = "eventlens.private-news-evidence.v1"
PUBLIC_NEWS_SCHEMA = "eventlens.public-news-evidence.v1"
_SUMMARY_NUMBER_RE = re.compile(
    r"(?:[$€£]\s?\d[\d,.]*|\d[\d,.]*\s?(?:%|percent|million|billion|m|bn))",
    re.IGNORECASE,
)
_SUMMARY_MATERIAL_RE = re.compile(
    r"\b(?:revenue|sales|earnings|eps|margin|profit|loss|guidance|outlook|"
    r"forecast|consensus|estimate|capex|cash flow|backlog|capacity|contract|"
    r"customer|demand|regulatory|risk|investigation)\b",
    re.IGNORECASE,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ProviderSummaryPhase(StrEnum):
    PRE_EVENT = "pre_event"
    POST_EVENT = "post_event"


class ProviderSummarySelectionReason(StrEnum):
    ISSUER_SPECIFIC = "issuer_specific"
    EXACT_TICKER = "exact_ticker"
    QUANTIFIED = "quantified"
    MATERIAL_TOPIC = "material_topic"
    PROVIDER_REASONING = "provider_reasoning"


class ProviderSummaryReference(_Frozen):
    """Private, checksum-addressable pointer to one bounded provider summary."""

    article_id: str = Field(min_length=1)
    article_metadata_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    published_at: datetime
    phase: ProviderSummaryPhase
    provider_native_sentiment_label: str | None = None
    selection_score: int = Field(ge=1)
    selection_reasons: tuple[ProviderSummarySelectionReason, ...] = Field(
        min_length=1
    )

    @field_validator("published_at")
    @classmethod
    def reference_time_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider-summary publication time must be timezone-aware")
        return value.astimezone(UTC)


class LiveNewsEvidenceItem(_Frozen):
    article: NewsArticleMetadata
    content: ContentEvidence
    passages: PassageSelection

    @model_validator(mode="after")
    def bindings_match(self) -> LiveNewsEvidenceItem:
        article, content, passages = self.article, self.content, self.passages
        identity = (
            article.provider_article_id,
            article.item_sha256,
            article.discovery_fingerprint,
        )
        if identity != (
            content.provider_article_id,
            content.article_metadata_sha256,
            content.discovery_sha256,
        ) or identity != (
            passages.article_id,
            passages.article_metadata_sha256,
            passages.article_discovery_fingerprint,
        ):
            raise ValueError(
                "news metadata, content, and passage identities must match"
            )
        if (
            passages.content_policy_sha256 != content.policy_sha256
            or passages.evidence_sha256 != content.evidence_sha256
            or passages.grant_id != content.grant_id
            or passages.grant_version != content.grant_version
        ):
            raise ValueError("passage selection must bind the exact content grant")
        paragraph_by_id = {item.paragraph_id: item for item in content.paragraphs}
        excerpt_by_id = {item.paragraph_id: item for item in content.excerpts}
        for selected in passages.selected_passages:
            paragraph = paragraph_by_id.get(selected.paragraph_id)
            if paragraph is None or paragraph.text_sha256 != selected.paragraph_sha256:
                raise ValueError(
                    "selected passage does not resolve to its paragraph hash"
                )
            retained = excerpt_by_id.get(selected.paragraph_id)
            if retained is not None and (
                retained.text != selected.excerpt
                or retained.text_sha256 != selected.excerpt_sha256
            ):
                raise ValueError(
                    "selected passage does not match retained excerpt bytes"
                )
            if retained is None and (
                paragraph.text is None
                or bound_excerpt(paragraph.text, passages.policy.max_excerpt_characters)
                != selected.excerpt
            ):
                raise ValueError("selected passage text is not resolver-verifiable")
        return self


class PublicNewsEvidenceItem(_Frozen):
    article_ref_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    published_at: datetime
    source_domain: str | None = None
    ticker_match: NewsTickerMatch
    provider_native_sentiment_label: str | None = None
    content_status: ContentFetchStatus
    reason: ContentFetchReason
    access_status: ContentAccessStatus
    rights_status: ContentRightsStatus
    temporal_status: ContentTemporalStatus
    retention_policy: ContentRetentionPolicy
    grant_id: str
    grant_version: str
    content_evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    passage_selection_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected_passage_count: int = Field(ge=0)

    @field_validator("published_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must be timezone-aware")
        return value.astimezone(UTC)


class PublicNewsEvidenceProjection(_Frozen):
    schema_version: str = PUBLIC_NEWS_SCHEMA
    manifest_id: str
    private_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ticker: str
    cutoff_at: datetime
    items: tuple[PublicNewsEvidenceItem, ...]
    projection_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def canonical(self) -> PublicNewsEvidenceProjection:
        refs = tuple(item.article_ref_sha256 for item in self.items)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise ValueError("public news references must be sorted and unique")
        expected = sha256_hex(
            self.model_dump(mode="json", exclude={"projection_sha256"})
        )
        if self.projection_sha256 is None:
            object.__setattr__(self, "projection_sha256", expected)
        elif self.projection_sha256 != expected:
            raise ValueError("public news projection checksum does not match")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class LiveNewsEvidenceManifest(_Frozen):
    schema_version: str = PRIVATE_NEWS_SCHEMA
    manifest_id: str
    acceptance_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ticker: str
    cutoff_at: datetime
    collected_at: datetime
    provider_response_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    direct_fetch_count: int = Field(ge=0)
    items: tuple[LiveNewsEvidenceItem, ...]
    manifest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("cutoff_at", "collected_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("news manifest timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def canonical(self) -> LiveNewsEvidenceManifest:
        if self.collected_at >= self.cutoff_at:
            raise ValueError("private news evidence must be captured before cutoff")
        ids = tuple(item.article.provider_article_id for item in self.items)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("private news evidence items must be sorted and unique")
        if any(
            item.article.ticker != self.ticker
            or item.article.published_at >= self.cutoff_at
            or item.content.cutoff_at != self.cutoff_at
            for item in self.items
        ):
            raise ValueError("private news evidence must match ticker and cutoff")
        expected = sha256_hex(self.model_dump(mode="json", exclude={"manifest_sha256"}))
        if self.manifest_sha256 is None:
            object.__setattr__(self, "manifest_sha256", expected)
        elif self.manifest_sha256 != expected:
            raise ValueError("private news evidence manifest checksum does not match")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))

    def resolve_passage(self, article_id: str, passage_id: str) -> SelectedPassage:
        for item in self.items:
            if item.article.provider_article_id == article_id:
                for passage in item.passages.selected_passages:
                    if passage.paragraph_id == passage_id:
                        return passage
                break
        raise KeyError(f"unresolved news passage: {article_id}:{passage_id}")

    def resolve_provider_summary(self, reference: ProviderSummaryReference) -> str:
        """Resolve an exact private summary; public projections cannot do this."""

        for item in self.items:
            article = item.article
            if article.provider_article_id != reference.article_id:
                continue
            if article.item_sha256 != reference.article_metadata_sha256:
                break
            summary = article.provider_summary
            if (
                article.summary_status is not NewsSummaryStatus.RETAINED_PERMITTED
                or summary is None
                or _provider_summary_sha256(article) != reference.summary_sha256
            ):
                break
            return summary
        raise KeyError(f"unresolved provider summary: {reference.article_id}")

    def public_projection(self) -> PublicNewsEvidenceProjection:
        assert self.manifest_sha256 is not None
        items = tuple(
            sorted(
                (
                    PublicNewsEvidenceItem(
                        article_ref_sha256=sha256_hex(item.article.provider_article_id),
                        published_at=item.article.published_at,
                        source_domain=item.article.source_domain,
                        ticker_match=item.article.ticker_match,
                        provider_native_sentiment_label=item.article.ticker_sentiment_label,
                        content_status=item.content.status,
                        reason=item.content.reason,
                        access_status=item.content.access_status,
                        rights_status=item.content.rights_status,
                        temporal_status=item.content.temporal_status,
                        retention_policy=item.content.retention_policy,
                        grant_id=item.content.grant_id,
                        grant_version=item.content.grant_version,
                        content_evidence_sha256=item.content.evidence_sha256
                        or item.content.content_checksum(),
                        passage_selection_sha256=item.passages.selection_sha256 or "",
                        selected_passage_count=len(item.passages.selected_passages),
                    )
                    for item in self.items
                ),
                key=lambda item: item.article_ref_sha256,
            )
        )
        return PublicNewsEvidenceProjection(
            manifest_id=self.manifest_id,
            private_manifest_sha256=self.manifest_sha256,
            ticker=self.ticker,
            cutoff_at=self.cutoff_at,
            items=items,
        )


def build_live_news_evidence(
    contract: LiveEvidenceContract,
    retained: RetainedLiveEvidence,
    *,
    policies_by_host: Mapping[str, ContentFetchPolicy] | None = None,
    fetcher: RightsAwareContentFetcher | None = None,
) -> LiveNewsEvidenceManifest:
    """Build the private resolver manifest; absent grants cause no network call."""

    if (
        retained.manifest_id != contract.manifest_id
        or retained.manifest_sha256 != contract.manifest_sha256
        or retained.ticker != contract.ticker
        or retained.information_cutoff_at != contract.information_cutoff_at
    ):
        raise ValueError("retained evidence does not match the live news contract")
    news = retained.news
    return build_news_evidence_manifest(
        manifest_id=f"{contract.manifest_id}:private-news-v1",
        acceptance_manifest_sha256=contract.manifest_sha256,
        ticker=contract.ticker,
        cutoff_at=contract.information_cutoff_at,
        collected_at=retained.collected_at,
        provider_response_sha256=(
            news.provenance.raw_response_sha256 if news else None
        ),
        articles=() if news is None else news.data.articles,
        issuer_terms=(
            contract.ticker,
            contract.legal_name,
            contract.legal_name.split()[0],
        ),
        maximum_direct_fetches=(
            contract.hard_total_request_ceiling - retained.budget.total_reserved
        ),
        policies_by_host=policies_by_host,
        fetcher=fetcher,
    )


def build_news_evidence_manifest(
    *,
    manifest_id: str,
    acceptance_manifest_sha256: str,
    ticker: str,
    cutoff_at: datetime,
    collected_at: datetime,
    provider_response_sha256: str | None,
    articles: tuple[NewsArticleMetadata, ...],
    issuer_terms: tuple[str, ...],
    maximum_direct_fetches: int,
    policies_by_host: Mapping[str, ContentFetchPolicy] | None = None,
    fetcher: RightsAwareContentFetcher | None = None,
) -> LiveNewsEvidenceManifest:
    """Build one resolver manifest without coupling it to a live collection."""

    policies = {
        host.casefold().removeprefix("www."): value
        for host, value in (policies_by_host or {}).items()
    }
    items: list[LiveNewsEvidenceItem] = []
    direct_fetch_count = sum(
        1
        for article in articles
        if (
            policy := policies.get(
                (urlparse(article.url).hostname or "").casefold().removeprefix("www.")
            )
        )
        is not None
        and policy.access_permitted
    )
    if direct_fetch_count > maximum_direct_fetches:
        raise ValueError("content policies exceed the direct-fetch allowance")
    for article in articles:
        host = (urlparse(article.url).hostname or "").casefold().removeprefix("www.")
        explicit_policy = policies.get(host)
        retained_provider_summary = (
            article.summary_status is NewsSummaryStatus.RETAINED_PERMITTED
            and article.provider_summary is not None
        )
        summary_grant_id = article.provider_fields.get("summary_grant_id")
        summary_grant_version = article.provider_fields.get("summary_grant_version")
        if retained_provider_summary and not (
            isinstance(summary_grant_id, str)
            and summary_grant_id
            and isinstance(summary_grant_version, str)
            and summary_grant_version
        ):
            raise ValueError("retained provider summary requires a versioned grant")
        policy = explicit_policy or ContentFetchPolicy(
            source_kind=(
                ContentSourceKind.PROVIDER
                if retained_provider_summary
                else ContentSourceKind.THIRD_PARTY
            ),
            grant_id=(
                summary_grant_id
                if retained_provider_summary
                else "eventlens-metadata-only"
            ),
            grant_version=(
                summary_grant_version if retained_provider_summary else "v1"
            ),
            source_ref=(
                f"provider-summary:{article.provider_article_id}"
                if retained_provider_summary
                else f"acceptance-manifest:{acceptance_manifest_sha256}"
            ),
            allowed_hosts=(host,),
            retention_policy=ContentRetentionPolicy.METADATA_ONLY,
        )
        selection_policy = (
            PassageSelectionPolicy(
                max_passages_per_article=max(1, min(3, policy.max_excerpt_count)),
                max_excerpt_characters=policy.max_excerpt_characters,
                max_total_excerpt_characters=min(
                    10_000,
                    policy.max_excerpt_count * policy.max_excerpt_characters,
                ),
            )
            if explicit_policy is not None
            and policy.access_permitted
            and policy.max_excerpt_count > 0
            and policy.max_excerpt_characters >= 80
            else PassageSelectionPolicy()
        )
        selector = passage_excerpt_selector(
            issuer_terms=issuer_terms,
            policy=selection_policy,
        )
        content = (fetcher or RightsAwareContentFetcher()).fetch(
            article,
            cutoff_at=cutoff_at,
            policy=policy,
            excerpt_selector=selector,
        )
        passages = select_content_passages(
            content,
            article=article,
            fetch_policy=policy,
            issuer_terms=issuer_terms,
            policy=selection_policy,
        )
        items.append(
            LiveNewsEvidenceItem(article=article, content=content, passages=passages)
        )
    return LiveNewsEvidenceManifest(
        manifest_id=manifest_id,
        acceptance_manifest_sha256=acceptance_manifest_sha256,
        ticker=ticker,
        cutoff_at=cutoff_at,
        collected_at=collected_at,
        provider_response_sha256=provider_response_sha256,
        direct_fetch_count=direct_fetch_count,
        items=tuple(sorted(items, key=lambda item: item.article.provider_article_id)),
    )


def select_provider_summary_references(
    manifest: LiveNewsEvidenceManifest,
    *,
    event_at: datetime,
    issuer_terms: tuple[str, ...],
    maximum_pre_event_summaries: int = 3,
    allow_post_event_explanations: bool = False,
    maximum_post_event_summaries: int = 2,
) -> tuple[ProviderSummaryReference, ...]:
    """Select bounded direct-summary pointers without inferring article bodies.

    Pre-event summaries are always eligible.  Post-event summaries require an
    explicit, independently evaluated anomaly gate from the caller.
    """

    if event_at.tzinfo is None or event_at.utcoffset() is None:
        raise ValueError("event_at must be timezone-aware")
    if not 0 <= maximum_pre_event_summaries <= 10:
        raise ValueError("maximum_pre_event_summaries must be between zero and ten")
    if not 0 <= maximum_post_event_summaries <= 10:
        raise ValueError("maximum_post_event_summaries must be between zero and ten")
    event_at = event_at.astimezone(UTC)
    ranked: dict[ProviderSummaryPhase, list[ProviderSummaryReference]] = {
        ProviderSummaryPhase.PRE_EVENT: [],
        ProviderSummaryPhase.POST_EVENT: [],
    }
    normalized_terms = tuple(term.casefold().strip() for term in issuer_terms if term.strip())
    for item in manifest.items:
        article = item.article
        summary = article.provider_summary
        if (
            item.content.status is not ContentFetchStatus.SUMMARY_ONLY
            or article.summary_status is not NewsSummaryStatus.RETAINED_PERMITTED
            or summary is None
        ):
            continue
        phase = (
            ProviderSummaryPhase.PRE_EVENT
            if article.published_at < event_at
            else ProviderSummaryPhase.POST_EVENT
        )
        if phase is ProviderSummaryPhase.POST_EVENT and not allow_post_event_explanations:
            continue
        folded = summary.casefold()
        reasons: list[ProviderSummarySelectionReason] = []
        if any(term in folded for term in normalized_terms):
            reasons.append(ProviderSummarySelectionReason.ISSUER_SPECIFIC)
        if article.ticker_match is NewsTickerMatch.EXACT:
            reasons.append(ProviderSummarySelectionReason.EXACT_TICKER)
        if _SUMMARY_NUMBER_RE.search(summary):
            reasons.append(ProviderSummarySelectionReason.QUANTIFIED)
        if _SUMMARY_MATERIAL_RE.search(summary):
            reasons.append(ProviderSummarySelectionReason.MATERIAL_TOPIC)
        if "ticker sentiment reasoning:" in folded:
            reasons.append(ProviderSummarySelectionReason.PROVIDER_REASONING)
        if not reasons:
            continue
        weights = {
            ProviderSummarySelectionReason.ISSUER_SPECIFIC: 5,
            ProviderSummarySelectionReason.EXACT_TICKER: 4,
            ProviderSummarySelectionReason.QUANTIFIED: 3,
            ProviderSummarySelectionReason.MATERIAL_TOPIC: 2,
            ProviderSummarySelectionReason.PROVIDER_REASONING: 1,
        }
        ranked[phase].append(
            ProviderSummaryReference(
                article_id=article.provider_article_id,
                article_metadata_sha256=article.item_sha256 or "",
                summary_sha256=_provider_summary_sha256(article),
                published_at=article.published_at,
                phase=phase,
                provider_native_sentiment_label=article.ticker_sentiment_label,
                selection_score=sum(weights[reason] for reason in reasons),
                selection_reasons=tuple(reasons),
            )
        )
    for values in ranked.values():
        values.sort(
            key=lambda item: (
                -item.selection_score,
                -item.published_at.timestamp(),
                item.article_id,
            )
        )
    return tuple(
        ranked[ProviderSummaryPhase.PRE_EVENT][:maximum_pre_event_summaries]
        + ranked[ProviderSummaryPhase.POST_EVENT][:maximum_post_event_summaries]
    )


def _provider_summary_sha256(article: NewsArticleMetadata) -> str:
    if article.provider_summary is None or article.item_sha256 is None:
        raise ValueError("provider summary reference requires normalized summary metadata")
    return sha256_hex(
        {
            "article_metadata_sha256": article.item_sha256,
            "summary": article.provider_summary,
        }
    )


__all__ = [
    "LiveNewsEvidenceItem",
    "LiveNewsEvidenceManifest",
    "ProviderSummaryPhase",
    "ProviderSummaryReference",
    "ProviderSummarySelectionReason",
    "PublicNewsEvidenceProjection",
    "build_live_news_evidence",
    "build_news_evidence_manifest",
    "select_provider_summary_references",
]
