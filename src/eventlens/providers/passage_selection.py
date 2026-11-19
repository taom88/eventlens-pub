"""Deterministic selection of material article passages and duplicate clusters.

This module deliberately does not classify sentiment or draw an investment
implication.  It makes the bounded, auditable evidence set that a later
classifier may inspect.  Extraction and rights handling remain in
``eventlens.providers.content``; callers only pass text that is permitted for
the requested output.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from eventlens.ontologies.schema import ResolvedOntology

from .content import (
    bound_excerpt,
    ContentEvidence,
    ContentFetchPolicy,
    ContentFetchStatus,
    ContentParagraph,
)
from .models import NewsArticleMetadata


_SELECTION_VERSION = "eventlens.passage-selection.v1"
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)?")
_NUMBER_RE = re.compile(r"(?:[$€£]\s?\d[\d,.]*|\d[\d,.]*\s?(?:%|percent|million|billion|m|bn))", re.I)
_QUANTIFIED_WORD_RE = re.compile(
    r"\b(?:revenue|sales|earnings|eps|margin|profit|loss|capex|cash flow|backlog|shipments?)\b",
    re.I,
)
_GUIDANCE_RE = re.compile(
    r"\b(?:guidance|outlook|forecast|expects?|projects?|targets?|reaffirm(?:ed|s)?)\b",
    re.I,
)
_MANAGEMENT_RE = re.compile(
    r"[\"“”]|\b(?:ceo|cfo|chief executive|chief financial|said|according to management)\b",
    re.I,
)
_CONSENSUS_RE = re.compile(
    r"\b(?:consensus|analysts?|estimate[sd]?|wall street|price target|downgrad(?:e|ed)|upgrad(?:e|ed))\b",
    re.I,
)
_RISK_RE = re.compile(
    r"\b(?:risk|warn(?:ed|ing)?|headwind|weak(?:ness)?|slowdown|uncertainty|investigation|lawsuit|recall|shortage|pressure)\b",
    re.I,
)
_UP_RE = re.compile(r"\b(?:raise[ds]?|increas(?:e[ds]?|ing)|boost(?:ed|s)?|above)\b", re.I)
_DOWN_RE = re.compile(r"\b(?:cut(?:s)?|lower(?:ed|s)?|decreas(?:e[ds]?|ing)|reduce[ds]?|below)\b", re.I)
_METRIC_RE = re.compile(r"\b(?:guidance|revenue|sales|eps|earnings|consensus|estimate[sd]?|margin)\b", re.I)


class PassageSelectionReason(StrEnum):
    """Observable signals that caused a retained excerpt to rank highly."""

    ISSUER_SPECIFIC = "issuer_specific"
    QUANTIFIED_FACT = "quantified_fact"
    GUIDANCE = "guidance"
    MANAGEMENT_QUOTE = "management_quote"
    CONSENSUS_CHANGE = "consensus_change"
    MATERIAL_RISK = "material_risk"
    ONTOLOGY_CRITICAL = "ontology_critical"
    ONTOLOGY_HIGH = "ontology_high"


class DuplicateKind(StrEnum):
    EXACT = "exact"
    NEAR = "near"


class PassageSelectionPolicy(BaseModel):
    """Versioned bounds for deterministic passage retention."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = _SELECTION_VERSION
    max_passages_per_article: int = Field(default=3, ge=1, le=10)
    max_excerpt_characters: int = Field(default=800, ge=80, le=4_000)
    # This is intentionally lower than 3 * 800.  The selector skips a later
    # passage rather than truncating it again, so the stored MVP-305 excerpt is
    # byte-for-byte the excerpt whose score/reasons were audited here.
    max_total_excerpt_characters: int = Field(default=2_000, ge=80, le=10_000)
    near_duplicate_threshold: float = Field(default=0.78, ge=0.5, le=1.0)
    max_duplicate_age_days: int = Field(default=7, ge=0, le=31)

class PassageCandidate(BaseModel):
    """A rights-cleared paragraph projection supplied by the content layer."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    paragraph_id: str = Field(min_length=1)
    paragraph_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    ordinal: int = Field(ge=1)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def populate_or_validate_sha256(self) -> "PassageCandidate":
        expected = _sha256(self.text)
        if self.paragraph_sha256 is None:
            object.__setattr__(self, "paragraph_sha256", expected)
        elif self.paragraph_sha256 != expected:
            raise ValueError("paragraph_sha256 does not match paragraph text")
        return self


class SelectedPassage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paragraph_id: str
    paragraph_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    excerpt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ordinal: int = Field(ge=1)
    excerpt: str = Field(min_length=1)
    score: int = Field(ge=0)
    selection_reasons: tuple[PassageSelectionReason, ...]
    ontology_slot_ids: tuple[str, ...] = ()


class PassageSelection(BaseModel):
    """The bounded selection for a single discovered article."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    article_id: str = Field(min_length=1)
    article_metadata_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    article_discovery_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    content_policy_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    grant_id: str | None = None
    grant_version: str | None = None
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    policy: PassageSelectionPolicy = PassageSelectionPolicy()
    policy_version: str = _SELECTION_VERSION
    selected_passages: tuple[SelectedPassage, ...]
    candidate_count: int = Field(ge=0)
    selection_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def passage_ids_are_unique(self) -> "PassageSelection":
        ids = [item.paragraph_id for item in self.selected_passages]
        if len(ids) != len(set(ids)):
            raise ValueError("selected passages cannot repeat paragraph IDs")
        if self.policy_version != self.policy.version:
            raise ValueError("policy_version must match embedded selection policy version")
        if len(self.selected_passages) > self.policy.max_passages_per_article:
            raise ValueError("selected passage count exceeds embedded policy")
        if self.candidate_count < len(self.selected_passages):
            raise ValueError("candidate_count cannot be smaller than selected passage count")
        if sum(len(item.excerpt) for item in self.selected_passages) > self.policy.max_total_excerpt_characters:
            raise ValueError("selected excerpts exceed embedded total character bound")
        reason_rank = {reason: index for index, reason in enumerate(PassageSelectionReason)}
        for item in self.selected_passages:
            if not item.selection_reasons:
                raise ValueError("selected passages require at least one selection reason")
            if item.score != _score(item.selection_reasons):
                raise ValueError("selected passage score does not match selection reasons")
            if len(item.excerpt) > self.policy.max_excerpt_characters:
                raise ValueError("selected excerpt exceeds embedded per-passage character bound")
            if item.excerpt != bound_excerpt(item.excerpt, self.policy.max_excerpt_characters):
                raise ValueError("selected excerpt is not canonically bounded")
            if item.excerpt_sha256 != _sha256(item.excerpt):
                raise ValueError("selected excerpt_sha256 does not match selected excerpt")
            if tuple(sorted(item.selection_reasons, key=reason_rank.__getitem__)) != item.selection_reasons:
                raise ValueError("selection reasons must be canonical and sorted")
            if len(set(item.selection_reasons)) != len(item.selection_reasons):
                raise ValueError("selection reasons cannot contain duplicates")
            if tuple(sorted(item.ontology_slot_ids)) != item.ontology_slot_ids or len(set(item.ontology_slot_ids)) != len(item.ontology_slot_ids):
                raise ValueError("ontology slot IDs must be canonical and unique")
        canonical_order = tuple(sorted(
            self.selected_passages,
            key=lambda item: (-item.score, item.ordinal, item.paragraph_id, item.paragraph_sha256),
        ))
        if canonical_order != self.selected_passages:
            raise ValueError("selected passages must be in canonical score order")
        expected = sha256_hex(self.model_dump(mode="json", exclude={"selection_sha256"}))
        if self.selection_sha256 is None:
            object.__setattr__(self, "selection_sha256", expected)
        elif self.selection_sha256 != expected:
            raise ValueError("selection_sha256 does not match the selected passages")
        return self


class ArticleCandidate(BaseModel):
    """Small, retention-safe projection used only for duplicate detection."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    article_id: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    text: str | None = None
    source_domain: str | None = None
    source_url: str | None = None
    discovery_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    permitted_excerpt_sha256s: tuple[str, ...] = ()
    published_at: datetime
    content_status: str = "content_available"

    @field_validator("published_at")
    @classmethod
    def published_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class DuplicateCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_id: str = Field(pattern=r"^content-cluster:[a-f0-9]{16}$")
    kind: DuplicateKind
    representative_article_id: str = Field(min_length=1)
    article_ids: tuple[str, ...] = Field(min_length=2)
    member_discovery_fingerprints: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def member_ids_are_sorted_and_unique(self) -> "DuplicateCluster":
        if tuple(sorted(self.article_ids)) != self.article_ids:
            raise ValueError("cluster article IDs must be canonically sorted")
        if len(set(self.article_ids)) != len(self.article_ids):
            raise ValueError("cluster article IDs cannot repeat")
        if self.representative_article_id not in self.article_ids:
            raise ValueError("representative article must be a cluster member")
        if tuple(sorted(self.member_discovery_fingerprints)) != self.member_discovery_fingerprints:
            raise ValueError("cluster discovery fingerprints must be canonically sorted")
        if len(self.member_discovery_fingerprints) != len(self.article_ids):
            raise ValueError("cluster member fingerprints must match member count")
        return self


class DuplicateClustering(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = _SELECTION_VERSION
    policy: PassageSelectionPolicy = PassageSelectionPolicy()
    input_articles: tuple[ArticleCandidate, ...]
    clusters: tuple[DuplicateCluster, ...]
    unclustered_article_ids: tuple[str, ...]
    clustering_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def checksum_matches(self) -> "DuplicateClustering":
        if self.policy_version != self.policy.version:
            raise ValueError("policy_version must match embedded clustering policy version")
        ids = tuple(item.article_id for item in self.input_articles)
        fingerprints = tuple(item.discovery_fingerprint for item in self.input_articles)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("input article IDs must be canonical and unique")
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("input discovery fingerprints must be unique")
        if self.unclustered_article_ids != tuple(sorted(self.unclustered_article_ids)):
            raise ValueError("unclustered article IDs must be canonically sorted")
        if len(set(self.unclustered_article_ids)) != len(self.unclustered_article_ids):
            raise ValueError("unclustered article IDs must be unique")
        if tuple(sorted(self.clusters, key=lambda item: item.cluster_id)) != self.clusters:
            raise ValueError("duplicate clusters must be canonically sorted")
        by_id = {item.article_id: item for item in self.input_articles}
        clustered_ids: list[str] = []
        for cluster in self.clusters:
            if any(item not in by_id for item in cluster.article_ids):
                raise ValueError("duplicate cluster has an unknown member")
            expected_fingerprints = tuple(sorted(by_id[item].discovery_fingerprint for item in cluster.article_ids))
            if cluster.member_discovery_fingerprints != expected_fingerprints:
                raise ValueError("duplicate cluster fingerprints do not match input identities")
            expected_id = f"content-cluster:{_sha256('|'.join(expected_fingerprints))[:16]}"
            if cluster.cluster_id != expected_id:
                raise ValueError("duplicate cluster ID does not match its member fingerprints")
            members = [by_id[item] for item in cluster.article_ids]
            relations = [
                _duplicate_kind(left, right, self.policy.near_duplicate_threshold, self.policy.max_duplicate_age_days)
                for index, left in enumerate(members)
                for right in members[index + 1:]
            ]
            if any(relation is None for relation in relations):
                raise ValueError("duplicate cluster contains non-duplicate or conflicting members")
            expected_kind = DuplicateKind.EXACT if all(relation is DuplicateKind.EXACT for relation in relations) else DuplicateKind.NEAR
            if cluster.kind is not expected_kind:
                raise ValueError("duplicate cluster kind does not match input content")
            if cluster.representative_article_id != _representative(members).article_id:
                raise ValueError("duplicate cluster representative does not match member priority")
            clustered_ids.extend(cluster.article_ids)
        if len(set(clustered_ids)) != len(clustered_ids):
            raise ValueError("an article may belong to only one duplicate cluster")
        if set(clustered_ids) & set(self.unclustered_article_ids):
            raise ValueError("clustered and unclustered article IDs must be disjoint")
        if set(clustered_ids) | set(self.unclustered_article_ids) != set(ids):
            raise ValueError("duplicate clustering membership must cover every input article")
        expected_clusters, expected_unclustered = _deterministic_partition(self.input_articles, self.policy)
        if self.clusters != expected_clusters or self.unclustered_article_ids != expected_unclustered:
            raise ValueError("duplicate clustering does not match the deterministic input partition")
        expected = sha256_hex(self.model_dump(mode="json", exclude={"clustering_sha256"}))
        if self.clustering_sha256 is None:
            object.__setattr__(self, "clustering_sha256", expected)
        elif self.clustering_sha256 != expected:
            raise ValueError("clustering_sha256 does not match duplicate clusters")
        return self


def select_passages(
    *,
    article_id: str,
    paragraphs: Iterable[PassageCandidate],
    issuer_terms: Iterable[str],
    ontology: ResolvedOntology | None = None,
    policy: PassageSelectionPolicy = PassageSelectionPolicy(),
    issuer_context: bool = False,
    article_metadata_sha256: str | None = None,
    article_discovery_fingerprint: str | None = None,
    content_policy_sha256: str | None = None,
    grant_id: str | None = None,
    grant_version: str | None = None,
    evidence_sha256: str | None = None,
) -> PassageSelection:
    """Select material paragraphs without privileging their document position.

    Scoring is intentionally transparent and additive.  A paragraph with no
    signal is not selected merely because it is the lead; ties resolve by
    stable paragraph ID, not provider ordering.
    """

    terms = _normalized_terms(issuer_terms)
    candidates = tuple(paragraphs)
    if len({item.paragraph_id for item in candidates}) != len(candidates):
        raise ValueError("paragraph IDs must be unique within an article")

    scored: list[SelectedPassage] = []
    for item in candidates:
        reasons, ontology_slots = _selection_reasons(item.text, terms, ontology)
        if not reasons or (not issuer_context and (
            PassageSelectionReason.ISSUER_SPECIFIC not in reasons
            and not ontology_slots
        )):
            continue
        scored.append(
            SelectedPassage(
                paragraph_id=item.paragraph_id,
                paragraph_sha256=item.paragraph_sha256 or _sha256(item.text),
                excerpt_sha256=_sha256(bound_excerpt(item.text, policy.max_excerpt_characters)),
                ordinal=item.ordinal,
                excerpt=bound_excerpt(item.text, policy.max_excerpt_characters),
                score=_score(reasons),
                selection_reasons=reasons,
                ontology_slot_ids=ontology_slots,
            )
        )

    ordered = sorted(
        scored,
        key=lambda item: (-item.score, item.ordinal, item.paragraph_id, item.paragraph_sha256),
    )
    selected: list[SelectedPassage] = []
    used_characters = 0
    for item in ordered:
        if len(selected) >= policy.max_passages_per_article:
            break
        if used_characters + len(item.excerpt) > policy.max_total_excerpt_characters:
            continue
        selected.append(item)
        used_characters += len(item.excerpt)
    return PassageSelection(
        article_id=article_id,
        article_metadata_sha256=article_metadata_sha256,
        article_discovery_fingerprint=article_discovery_fingerprint,
        content_policy_sha256=content_policy_sha256,
        grant_id=grant_id,
        grant_version=grant_version,
        evidence_sha256=evidence_sha256,
        policy_version=policy.version,
        policy=policy,
        selected_passages=tuple(selected),
        candidate_count=len(candidates),
    )


def select_content_passages(
    evidence: ContentEvidence,
    *,
    article: NewsArticleMetadata,
    fetch_policy: ContentFetchPolicy,
    issuer_terms: Iterable[str],
    ontology: ResolvedOntology | None = None,
    policy: PassageSelectionPolicy = PassageSelectionPolicy(),
    issuer_context: bool = False,
) -> PassageSelection:
    """Apply selection to MVP-305 paragraph receipts under its exact grant.

    A policy mismatch means the caller cannot establish that the requested
    excerpts were permitted.  We fail closed rather than silently loosening the
    article's retention grant.  For bounded-excerpt evidence the fetcher's
    ``excerpt_selector`` hook must have selected the material paragraphs while
    all parsed text was transiently available; retained excerpts are then
    re-scored only to expose their reasons.
    """

    if article.provider_article_id != evidence.provider_article_id:
        raise ValueError("article metadata and content evidence IDs do not match")
    if article.item_sha256 != evidence.article_metadata_sha256:
        raise ValueError("article metadata checksum does not match content evidence")
    if (article.discovery_fingerprint or article.canonical_discovery_fingerprint()) != evidence.discovery_sha256:
        raise ValueError("article discovery fingerprint does not match content evidence")
    if evidence.policy_receipt != fetch_policy:
        raise ValueError("content evidence policy receipt does not match the supplied fetch policy")
    if evidence.status is not ContentFetchStatus.CONTENT_AVAILABLE:
        return PassageSelection(
            article_id=evidence.provider_article_id,
            article_metadata_sha256=evidence.article_metadata_sha256,
            article_discovery_fingerprint=evidence.discovery_sha256,
            content_policy_sha256=evidence.policy_sha256,
            grant_id=evidence.grant_id,
            grant_version=evidence.grant_version,
            evidence_sha256=evidence.evidence_sha256,
            policy_version=policy.version,
            policy=policy,
            selected_passages=(),
            candidate_count=0,
        )
    _validate_policy_against_grant(policy, fetch_policy)
    text_by_id = {item.paragraph_id: item.text for item in evidence.paragraphs if item.text}
    text_by_id.update({item.paragraph_id: item.text for item in evidence.excerpts})
    ordinal_by_id = {item.paragraph_id: item.ordinal for item in evidence.paragraphs}
    return select_passages(
        article_id=evidence.provider_article_id,
        paragraphs=tuple(
            PassageCandidate(
                paragraph_id=paragraph_id,
                paragraph_sha256=_sha256(text),
                ordinal=ordinal_by_id[paragraph_id],
                text=text,
            )
            for paragraph_id, text in sorted(text_by_id.items(), key=lambda pair: ordinal_by_id[pair[0]])
        ),
        issuer_terms=issuer_terms,
        ontology=ontology,
        policy=policy,
        article_metadata_sha256=evidence.article_metadata_sha256,
        article_discovery_fingerprint=evidence.discovery_sha256,
        content_policy_sha256=evidence.policy_sha256,
        grant_id=evidence.grant_id,
        grant_version=evidence.grant_version,
        evidence_sha256=evidence.evidence_sha256,
        issuer_context=issuer_context,
    )


class PassageExcerptSelector:
    """MVP-305 callback plus the matching audited selection result.

    Use one instance for ``RightsAwareContentFetcher.fetch`` and then call
    :meth:`selection_for` with its returned evidence.  The method verifies
    that the persisted excerpt bytes/hashes exactly match what was selected
    from the transient all-paragraph view.
    """

    def __init__(
        self,
        *,
        issuer_terms: Iterable[str],
        issuer_context: bool = False,
        ontology: ResolvedOntology | None = None,
        policy: PassageSelectionPolicy = PassageSelectionPolicy(),
    ) -> None:
        self._terms = tuple(issuer_terms)
        self._issuer_context = issuer_context
        self._ontology = ontology
        self._policy = policy
        self._selections: dict[tuple[str, ...], PassageSelection] = {}
        self._grants: dict[tuple[str, ...], ContentFetchPolicy] = {}

    def __call__(
        self, paragraphs: tuple[ContentParagraph, ...], fetch_policy: ContentFetchPolicy
    ) -> tuple[str, ...]:
        _validate_policy_against_grant(self._policy, fetch_policy)
        selection = select_passages(
            article_id="transient-content",
            paragraphs=tuple(
                PassageCandidate(
                    paragraph_id=item.paragraph_id,
                    paragraph_sha256=item.text_sha256,
                    ordinal=item.ordinal,
                    text=item.text or "",
                )
                for item in paragraphs
                if item.text is not None
            ),
            issuer_terms=self._terms,
            ontology=self._ontology,
            policy=self._policy,
            issuer_context=self._issuer_context,
        )
        key = tuple(item.paragraph_id for item in paragraphs)
        self._selections[key] = selection
        self._grants[key] = fetch_policy
        return tuple(item.paragraph_id for item in selection.selected_passages)

    def selection_for(
        self, article: NewsArticleMetadata, evidence: ContentEvidence
    ) -> PassageSelection:
        if article.provider_article_id != evidence.provider_article_id:
            raise ValueError("article ID does not match content evidence")
        if article.item_sha256 != evidence.article_metadata_sha256:
            raise ValueError("article metadata checksum does not match content evidence")
        discovery = article.discovery_fingerprint or article.canonical_discovery_fingerprint()
        if discovery != evidence.discovery_sha256:
            raise ValueError("article discovery fingerprint does not match content evidence")
        key = tuple(item.paragraph_id for item in evidence.paragraphs)
        selection = self._selections.get(key)
        grant = self._grants.get(key)
        if selection is None or grant is None:
            raise ValueError("no transient passage selection is available for this evidence")
        if evidence.policy_sha256 != grant.canonical_sha256():
            raise ValueError("content evidence policy does not match selection grant")
        if evidence.grant_id != grant.grant_id or evidence.grant_version != grant.grant_version:
            raise ValueError("content evidence grant does not match selection grant")
        if evidence.evidence_sha256 != evidence.content_checksum():
            raise ValueError("content evidence checksum does not match selection receipt")
        expected = {item.paragraph_id: item for item in selection.selected_passages}
        actual = {item.paragraph_id: item for item in evidence.excerpts}
        if set(expected) != set(actual) or any(
            actual[paragraph_id].text != selected.excerpt
            or actual[paragraph_id].text_sha256 != selected.excerpt_sha256
            for paragraph_id, selected in expected.items()
        ):
            raise ValueError("persisted excerpts do not exactly match the passage selection")
        return PassageSelection(
            article_id=article.provider_article_id,
            article_metadata_sha256=evidence.article_metadata_sha256,
            article_discovery_fingerprint=evidence.discovery_sha256,
            content_policy_sha256=evidence.policy_sha256,
            grant_id=evidence.grant_id,
            grant_version=evidence.grant_version,
            evidence_sha256=evidence.evidence_sha256,
            policy_version=selection.policy_version,
            policy=selection.policy,
            selected_passages=selection.selected_passages,
            candidate_count=selection.candidate_count,
        )


def passage_excerpt_selector(
    *,
    issuer_terms: Iterable[str],
    issuer_context: bool = False,
    ontology: ResolvedOntology | None = None,
    policy: PassageSelectionPolicy = PassageSelectionPolicy(),
):
    """Return the MVP-305 callback that picks bounded excerpts before discard.

    The callback receives every parsed paragraph, including middle passages,
    and returns only stable paragraph IDs as required by the content fetcher.
    It cannot request bounds looser than the fetch policy provided at runtime.
    """

    return PassageExcerptSelector(
        issuer_terms=issuer_terms,
        issuer_context=issuer_context,
        ontology=ontology,
        policy=policy,
    )


def article_candidate_from_content(
    article: NewsArticleMetadata, evidence: ContentEvidence
) -> ArticleCandidate:
    """Make the clustering projection while preserving discovery identity."""

    if article.provider_article_id != evidence.provider_article_id:
        raise ValueError("article metadata and content evidence IDs do not match")
    if article.item_sha256 != evidence.article_metadata_sha256:
        raise ValueError("article metadata checksum does not match content evidence")
    if (article.discovery_fingerprint or article.canonical_discovery_fingerprint()) != evidence.discovery_sha256:
        raise ValueError("article discovery fingerprint does not match content evidence")
    text = evidence.full_body
    if text is None and evidence.excerpts:
        ordinal = {item.paragraph_id: item.ordinal for item in evidence.paragraphs}
        text = "\n".join(
            item.text for item in sorted(evidence.excerpts, key=lambda item: ordinal[item.paragraph_id])
        )
    if text is None and evidence.paragraphs and all(item.text is not None for item in evidence.paragraphs):
        text = "\n".join(item.text or "" for item in evidence.paragraphs)
    return ArticleCandidate(
        article_id=article.provider_article_id,
        headline=article.headline,
        text=text,
        source_domain=article.source_domain,
        source_url=article.url,
        discovery_fingerprint=evidence.discovery_sha256,
        permitted_excerpt_sha256s=tuple(item.text_sha256 for item in evidence.excerpts),
        published_at=article.published_at,
        content_status=evidence.status.value,
    )


def cluster_duplicates(
    articles: Iterable[ArticleCandidate],
    *,
    policy: PassageSelectionPolicy = PassageSelectionPolicy(),
) -> DuplicateClustering:
    """Cluster exact and near syndicated stories in a reproducible order.

    Similar issuer stories with incompatible guidance direction or quantified
    values are deliberately kept apart.  A greedy all-members comparison
    avoids a transitive chain merging otherwise conflicting articles.
    """

    candidates = tuple(articles)
    ids = [item.article_id for item in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("article IDs must be unique for duplicate clustering")
    ordered = tuple(sorted(candidates, key=lambda item: item.article_id))
    clusters, unclustered = _deterministic_partition(ordered, policy)

    return DuplicateClustering(
        policy_version=policy.version,
        policy=policy,
        input_articles=ordered,
        clusters=clusters,
        unclustered_article_ids=unclustered,
    )


def _deterministic_partition(
    articles: tuple[ArticleCandidate, ...], policy: PassageSelectionPolicy
) -> tuple[tuple[DuplicateCluster, ...], tuple[str, ...]]:
    """The single canonical partition used for build and artifact validation."""

    ordered = tuple(sorted(articles, key=lambda item: item.article_id))
    remaining = set(item.article_id for item in ordered)
    by_id = {item.article_id: item for item in ordered}
    clusters: list[DuplicateCluster] = []
    for seed in ordered:
        if seed.article_id not in remaining:
            continue
        matches = [seed]
        relation_kinds: list[DuplicateKind] = []
        for candidate in ordered:
            if candidate.article_id not in remaining or candidate.article_id == seed.article_id:
                continue
            kind = _duplicate_kind(seed, candidate, policy.near_duplicate_threshold, policy.max_duplicate_age_days)
            if kind is not None and all(
                _duplicate_kind(member, candidate, policy.near_duplicate_threshold, policy.max_duplicate_age_days)
                is not None for member in matches
            ):
                matches.append(candidate)
                relation_kinds.append(kind)
        if len(matches) < 2:
            continue
        member_ids = tuple(sorted(item.article_id for item in matches))
        remaining.difference_update(member_ids)
        fingerprints = tuple(sorted(by_id[item_id].discovery_fingerprint for item_id in member_ids))
        clusters.append(DuplicateCluster(
            cluster_id=f"content-cluster:{_sha256('|'.join(fingerprints))[:16]}",
            kind=DuplicateKind.EXACT if all(kind is DuplicateKind.EXACT for kind in relation_kinds) else DuplicateKind.NEAR,
            representative_article_id=_representative(matches).article_id,
            article_ids=member_ids,
            member_discovery_fingerprints=fingerprints,
        ))
    return tuple(sorted(clusters, key=lambda item: item.cluster_id)), tuple(sorted(remaining))


def _selection_reasons(
    text: str, issuer_terms: tuple[str, ...], ontology: ResolvedOntology | None
) -> tuple[tuple[PassageSelectionReason, ...], tuple[str, ...]]:
    normalized = " ".join(_tokens(text))
    reasons: list[PassageSelectionReason] = []
    text_tokens = _tokens(text)
    if issuer_terms and any(_contains_phrase(text_tokens, term) for term in issuer_terms):
        reasons.append(PassageSelectionReason.ISSUER_SPECIFIC)
    if _NUMBER_RE.search(text) and _QUANTIFIED_WORD_RE.search(text):
        reasons.append(PassageSelectionReason.QUANTIFIED_FACT)
    if _GUIDANCE_RE.search(text):
        reasons.append(PassageSelectionReason.GUIDANCE)
    if _MANAGEMENT_RE.search(text):
        reasons.append(PassageSelectionReason.MANAGEMENT_QUOTE)
    if _CONSENSUS_RE.search(text):
        reasons.append(PassageSelectionReason.CONSENSUS_CHANGE)
    if _RISK_RE.search(text):
        reasons.append(PassageSelectionReason.MATERIAL_RISK)
    matched_slots: list[tuple[str, object]] = []
    if ontology is not None:
        for slot in ontology.slots:
            aliases = _normalized_terms((slot.slot_id, *slot.aliases))
            if any(_contains_phrase(text_tokens, alias) for alias in aliases):
                matched_slots.append((slot.slot_id, slot.materiality))
    if any(getattr(materiality, "value", materiality) == "critical" for _, materiality in matched_slots):
        reasons.append(PassageSelectionReason.ONTOLOGY_CRITICAL)
    elif any(getattr(materiality, "value", materiality) == "high" for _, materiality in matched_slots):
        reasons.append(PassageSelectionReason.ONTOLOGY_HIGH)
    return tuple(reasons), tuple(slot_id for slot_id, _ in sorted(matched_slots))


def _score(reasons: tuple[PassageSelectionReason, ...]) -> int:
    weights = {
        PassageSelectionReason.ISSUER_SPECIFIC: 3,
        PassageSelectionReason.QUANTIFIED_FACT: 4,
        PassageSelectionReason.GUIDANCE: 5,
        PassageSelectionReason.MANAGEMENT_QUOTE: 4,
        PassageSelectionReason.CONSENSUS_CHANGE: 4,
        PassageSelectionReason.MATERIAL_RISK: 4,
        PassageSelectionReason.ONTOLOGY_CRITICAL: 5,
        PassageSelectionReason.ONTOLOGY_HIGH: 3,
    }
    return sum(weights[reason] for reason in reasons)


def _duplicate_kind(
    left: ArticleCandidate, right: ArticleCandidate, threshold: float, max_age_days: int
) -> DuplicateKind | None:
    if abs(left.published_at - right.published_at) > timedelta(days=max_age_days):
        return None
    left_text = _document_text(left)
    right_text = _document_text(right)
    if _conflicts(left_text, right_text):
        return None
    if left.source_url is not None and left.source_url == right.source_url:
        return DuplicateKind.EXACT
    if _document_fingerprint(left) == _document_fingerprint(right):
        return DuplicateKind.EXACT
    left_tokens, right_tokens = _shingles(_tokens(left_text)), _shingles(_tokens(right_text))
    if not left_tokens or not right_tokens:
        return None
    similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return DuplicateKind.NEAR if similarity >= threshold else None


def _conflicts(left: str, right: str) -> bool:
    left_up, left_down = bool(_UP_RE.search(left)), bool(_DOWN_RE.search(left))
    right_up, right_down = bool(_UP_RE.search(right)), bool(_DOWN_RE.search(right))
    if (left_up and right_down) or (left_down and right_up):
        return True
    left_numbers = set(_normalized_numbers(left))
    right_numbers = set(_normalized_numbers(right))
    # Different stated values around guidance/expectations should not be
    # collapsed merely because most boilerplate is shared.
    topical = bool(_METRIC_RE.search(left) and _METRIC_RE.search(right))
    return topical and bool(left_numbers and right_numbers and left_numbers != right_numbers)


def _normalized_terms(terms: Iterable[str]) -> tuple[str, ...]:
    normalized = {" ".join(_tokens(item)) for item in terms if _tokens(item)}
    return tuple(sorted(normalized))


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(value.lower()))


def _contains_phrase(tokens: tuple[str, ...], phrase: str) -> bool:
    wanted = tuple(_tokens(phrase))
    return bool(wanted) and any(tokens[index:index + len(wanted)] == wanted for index in range(len(tokens) - len(wanted) + 1))


def _shingles(tokens: tuple[str, ...], size: int = 5) -> set[tuple[str, ...]]:
    if len(tokens) < size:
        return {tokens} if tokens else set()
    return {tokens[index:index + size] for index in range(len(tokens) - size + 1)}


def _representative(members: list[ArticleCandidate]) -> ArticleCandidate:
    quality = {"content_available": 0, "summary_only": 1, "headline_only": 2}
    return min(
        members,
        key=lambda item: (quality.get(item.content_status, 3), item.published_at, item.discovery_fingerprint, item.article_id),
    )


def _normalized_numbers(value: str) -> tuple[str, ...]:
    return tuple(sorted(re.sub(r"\s+", "", match.group(0).lower()) for match in _NUMBER_RE.finditer(value)))


def _document_text(article: ArticleCandidate) -> str:
    return article.text or article.headline


def _document_fingerprint(article: ArticleCandidate) -> str:
    return _sha256(" ".join(_tokens(_document_text(article))))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_hex(value: object) -> str:
    """The repository's canonical JSON checksum, kept local to avoid package cycles."""

    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_policy_against_grant(
    selection_policy: PassageSelectionPolicy, fetch_policy: ContentFetchPolicy
) -> None:
    if selection_policy.max_passages_per_article > fetch_policy.max_excerpt_count:
        raise ValueError("selection max_passages_per_article exceeds the content retention grant")
    if selection_policy.max_excerpt_characters != fetch_policy.max_excerpt_characters:
        raise ValueError("selection max_excerpt_characters must match the content retention grant")
    if selection_policy.max_total_excerpt_characters > (
        fetch_policy.max_excerpt_count * fetch_policy.max_excerpt_characters
    ):
        raise ValueError("selection max_total_excerpt_characters exceeds the content retention grant")


__all__ = [
    "ArticleCandidate",
    "DuplicateCluster",
    "DuplicateClustering",
    "DuplicateKind",
    "PassageCandidate",
    "PassageExcerptSelector",
    "PassageSelection",
    "PassageSelectionPolicy",
    "PassageSelectionReason",
    "SelectedPassage",
    "article_candidate_from_content",
    "cluster_duplicates",
    "passage_excerpt_selector",
    "select_content_passages",
    "select_passages",
]
