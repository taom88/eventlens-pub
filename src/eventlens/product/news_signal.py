"""Versioned deterministic aggregation of typed news classifications.

This module deliberately aggregates classifications; it does not infer a
classification, fetch content, or merge headline, content, and provider-native
sentiment into one ambiguous field.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventlens.agents.news import (
    NewsClassificationResult, NewsEntityMatch, NewsMateriality, NewsRelevance,
    NewsTone, NewsTopic,
)
from eventlens.artifacts.models import sha256_hex


NEWS_SIGNAL_CALCULATION_VERSION = "eventlens.news-signal.v1"
_ZERO = Decimal("0")
_ONE = Decimal("1")


class NewsCoverageQuality(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    NO_CONTENT = "no_content"


class NewsSignalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calculation_version: str = NEWS_SIGNAL_CALCULATION_VERSION
    recency_24h: Decimal = Decimal("1")
    recency_72h: Decimal = Decimal("0.8")
    recency_7d: Decimal = Decimal("0.6")
    recency_30d: Decimal = Decimal("0.4")
    recency_older: Decimal = Decimal("0.2")
    relevance_high: Decimal = Decimal("1")
    relevance_medium: Decimal = Decimal("0.6")
    relevance_low: Decimal = Decimal("0.25")
    materiality_high: Decimal = Decimal("1")
    materiality_medium: Decimal = Decimal("0.6")
    materiality_low: Decimal = Decimal("0.25")

    @field_validator("recency_24h", "recency_72h", "recency_7d", "recency_30d", "recency_older", "relevance_high", "relevance_medium", "relevance_low", "materiality_high", "materiality_medium", "materiality_low", mode="before")
    @classmethod
    def decimal_only(cls, value: object) -> object:
        # JSON round-trips encode Decimal as a string; reject binary floats,
        # which would otherwise introduce non-reproducible arithmetic.
        if isinstance(value, float) or not isinstance(value, (Decimal, str)):
            raise TypeError("news-signal policy factors must be Decimal values")
        return value

    @model_validator(mode="after")
    def factors_are_unit_interval(self) -> "NewsSignalPolicy":
        values = tuple(self.model_dump().values())[1:]
        if any(not isinstance(value, Decimal) or value < _ZERO or value > _ONE for value in values):
            raise ValueError("news-signal policy factors must be Decimal values in [0, 1]")
        return self


class NewsSignalInputReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    article_id: str = Field(min_length=1)
    discovery_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class NewsSignalItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    article_id: str
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    duplicate_cluster_id: str | None = None
    included: bool
    exclusion_reason: str | None = None
    recency_factor: Decimal
    relevance_factor: Decimal
    materiality_factor: Decimal
    confidence_factor: Decimal
    source_diversity_factor: Decimal
    weight: Decimal
    content_tone: NewsTone | None = None
    headline_tone: NewsTone
    provider_native_score: Decimal | None = None
    topics: tuple[NewsTopic, ...] = ()


class NewsTopicDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: NewsTopic
    weight: Decimal = Field(ge=0)


class NewsSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cutoff_at: datetime
    policy: NewsSignalPolicy
    calculation_version: str
    classifications: tuple[NewsClassificationResult, ...]
    input_receipts: tuple[NewsSignalInputReceipt, ...]
    items: tuple[NewsSignalItem, ...]
    weighted_content_score: Decimal | None = None
    weighted_headline_score: Decimal | None = None
    weighted_provider_native_score: Decimal | None = None
    positive_share: Decimal = Field(ge=0, le=1)
    negative_share: Decimal = Field(ge=0, le=1)
    mixed_share: Decimal = Field(ge=0, le=1)
    material_item_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    topic_distribution: tuple[NewsTopicDistribution, ...]
    contradiction_score: Decimal = Field(ge=0, le=1)
    duplicate_adjusted_breadth: int = Field(ge=0)
    abstention_rate: Decimal = Field(ge=0, le=1)
    coverage_quality: NewsCoverageQuality
    signal_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("cutoff_at")
    @classmethod
    def cutoff_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("news signal cutoff_at must use UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def canonical_artifact(self) -> "NewsSignal":
        if self.calculation_version != self.policy.calculation_version:
            raise ValueError("calculation_version must match embedded policy")
        _validate_inputs(self.classifications, self.cutoff_at)
        article_ids = tuple(item.request.article.provider_article_id for item in self.classifications)
        if article_ids != tuple(sorted(article_ids)):
            raise ValueError("embedded classifications must be canonically ordered")
        derived = _derive(self.classifications, self.cutoff_at, self.policy)
        for field, expected_value in derived.items():
            if getattr(self, field) != expected_value:
                raise ValueError(f"news signal {field} does not match deterministic inputs")
        receipt_ids = tuple(item.article_id for item in self.input_receipts)
        item_ids = tuple(item.article_id for item in self.items)
        if receipt_ids != tuple(sorted(receipt_ids)) or len(set(receipt_ids)) != len(receipt_ids):
            raise ValueError("input receipts must be sorted and unique by article ID")
        if item_ids != receipt_ids:
            raise ValueError("news signal items must match sorted input receipts")
        receipt_by_id = {item.article_id: item for item in self.input_receipts}
        for item in self.items:
            if item.result_sha256 != receipt_by_id[item.article_id].result_sha256:
                raise ValueError("news signal item result checksum does not match input receipt")
            factors = (item.recency_factor, item.relevance_factor, item.materiality_factor, item.confidence_factor, item.source_diversity_factor)
            if any(value < _ZERO or value > _ONE for value in factors):
                raise ValueError("news signal item factors must be in [0, 1]")
            if item.weight != item.recency_factor * item.relevance_factor * item.materiality_factor * item.confidence_factor * item.source_diversity_factor:
                raise ValueError("news signal item weight does not match recorded factors")
            if not item.included and (item.weight != _ZERO or item.source_diversity_factor != _ZERO):
                raise ValueError("excluded news signal item must have zero source adjustment and weight")
        if tuple(sorted(self.topic_distribution, key=lambda item: item.topic.value)) != self.topic_distribution:
            raise ValueError("topic distribution must be canonically ordered")
        expected = sha256_hex(self.model_dump(mode="json", exclude={"signal_sha256"}))
        if self.signal_sha256 is None:
            object.__setattr__(self, "signal_sha256", expected)
        elif self.signal_sha256 != expected:
            raise ValueError("signal_sha256 does not match news signal")
        return self


def aggregate_news_signal(
    classifications: Iterable[NewsClassificationResult], *, cutoff_at: datetime,
    policy: NewsSignalPolicy = NewsSignalPolicy(),
) -> NewsSignal:
    """Aggregate validated per-item classifications deterministically."""

    cutoff = _utc(cutoff_at)
    values = tuple(classifications)
    _validate_inputs(values, cutoff)
    ordered = tuple(sorted(values, key=lambda item: item.request.article.provider_article_id))
    derived = _derive(ordered, cutoff, policy)
    return NewsSignal(
        cutoff_at=cutoff, policy=policy, calculation_version=policy.calculation_version,
        classifications=ordered, **derived,
    )


def _derive(
    ordered: tuple[NewsClassificationResult, ...], cutoff: datetime, policy: NewsSignalPolicy,
) -> dict[str, object]:
    """Pure aggregation used for construction and artifact-load verification."""

    prelim = [_preliminary(item, cutoff, policy) for item in ordered]
    source_counts: dict[str, int] = {}
    for value in prelim:
        if value["included"]:
            source_counts[value["source"]] = source_counts.get(value["source"], 0) + 1
    items: list[NewsSignalItem] = []
    for value in prelim:
        source_factor = (_ONE / Decimal(source_counts[value["source"]])) if value["included"] else _ZERO
        weight = value["base_weight"] * source_factor
        items.append(NewsSignalItem(
            article_id=value["article_id"], result_sha256=value["result_sha256"],
            duplicate_cluster_id=value["cluster_id"], included=value["included"],
            exclusion_reason=value["exclusion_reason"], recency_factor=value["recency"],
            relevance_factor=value["relevance"], materiality_factor=value["materiality"],
            confidence_factor=value["confidence"], source_diversity_factor=source_factor,
            weight=weight, content_tone=value["content_tone"], headline_tone=value["headline_tone"],
            provider_native_score=value["provider_score"], topics=value["topics"],
        ))
    return _build_signal(ordered, tuple(items), policy)


def _build_signal(
    results: tuple[NewsClassificationResult, ...], items: tuple[NewsSignalItem, ...],
    policy: NewsSignalPolicy,
) -> dict[str, object]:
    included = tuple(item for item in items if item.included)
    content_items = tuple(item for item in included if item.content_tone not in {None, NewsTone.UNKNOWN})
    headline_items = tuple(item for item in included if item.headline_tone is not NewsTone.UNKNOWN)
    provider_items = tuple(item for item in included if item.provider_native_score is not None)
    content_score = _weighted_tone(content_items, lambda item: item.content_tone)
    headline_score = _weighted_tone(headline_items, lambda item: item.headline_tone)
    provider_score = _weighted_decimal(provider_items, lambda item: item.provider_native_score)
    denominator = sum((item.weight for item in content_items), _ZERO)
    shares = {
        tone: (sum((item.weight for item in content_items if item.content_tone is tone), _ZERO) / denominator if denominator else _ZERO)
        for tone in (NewsTone.POSITIVE, NewsTone.NEGATIVE, NewsTone.MIXED)
    }
    topic_weights: dict[NewsTopic, Decimal] = {}
    for item in included:
        if item.topics:
            split = item.weight / Decimal(len(item.topics))
            for topic in item.topics:
                topic_weights[topic] = topic_weights.get(topic, _ZERO) + split
    abstentions = sum(1 for item in results if item.abstention_reason is not None)
    material_count = sum(1 for item in included if item.materiality_factor >= policy.materiality_medium)
    quality = (NewsCoverageQuality.NO_CONTENT if not content_items else
               NewsCoverageQuality.COMPLETE if abstentions == 0 and len(content_items) == len(included) else
               NewsCoverageQuality.DEGRADED)
    receipts = tuple(NewsSignalInputReceipt(
        article_id=result.request.article.provider_article_id,
        discovery_sha256=result.request.article.discovery_fingerprint or result.request.article.canonical_discovery_fingerprint(),
        result_sha256=result.result_sha256 or "", request_sha256=result.request_sha256,
    ) for result in results)
    source_count = len({
        result.request.article.source_domain or result.request.article.source_name or "unknown"
        for result in results
        if next(item for item in items if item.article_id == result.request.article.provider_article_id).included
    })
    return {
        "input_receipts": receipts, "items": items,
        "weighted_content_score": content_score, "weighted_headline_score": headline_score,
        "weighted_provider_native_score": provider_score,
        "positive_share": shares[NewsTone.POSITIVE], "negative_share": shares[NewsTone.NEGATIVE],
        "mixed_share": shares[NewsTone.MIXED], "material_item_count": material_count,
        "source_count": source_count,
        "topic_distribution": tuple(NewsTopicDistribution(topic=topic, weight=weight) for topic, weight in sorted(topic_weights.items(), key=lambda pair: pair[0].value)),
        "contradiction_score": _contradiction(content_items, policy.materiality_medium), "duplicate_adjusted_breadth": len(included),
        "abstention_rate": (Decimal(abstentions) / Decimal(len(results)) if results else _ZERO),
        "coverage_quality": quality,
    }


def _preliminary(result: NewsClassificationResult, cutoff: datetime, policy: NewsSignalPolicy) -> dict[str, object]:
    article = result.request.article
    cluster = result.request.duplicate_cluster
    is_representative = cluster is None or cluster.representative_article_id == article.provider_article_id
    relevance = _relevance(result.relevance, policy)
    materiality = _materiality(result.materiality, policy)
    entity_ok = result.entity_match in {NewsEntityMatch.EXACT, NewsEntityMatch.RELATED}
    included = bool(is_representative and entity_ok and relevance > _ZERO and materiality > _ZERO)
    reason = None if included else (
        "duplicate_non_representative" if not is_representative else "irrelevant_or_entity_mismatch"
    )
    native = result.provider_native_sentiment.ticker_sentiment_score
    if native is None:
        native = result.provider_native_sentiment.overall_score
    return {
        "article_id": article.provider_article_id, "result_sha256": result.result_sha256 or "",
        "cluster_id": cluster.cluster_id if cluster else None, "included": included, "exclusion_reason": reason,
        "recency": _recency(cutoff - article.published_at, policy), "relevance": relevance,
        "materiality": materiality, "confidence": result.confidence,
        "base_weight": _recency(cutoff - article.published_at, policy) * relevance * materiality * result.confidence,
        "source": article.source_domain or article.source_name or "unknown", "content_tone": result.agent_content_tone,
        "headline_tone": result.agent_headline_tone, "provider_score": native, "topics": result.topics,
    }


def _validate_inputs(results: tuple[NewsClassificationResult, ...], cutoff: datetime) -> None:
    ids = [item.request.article.provider_article_id for item in results]
    if len(ids) != len(set(ids)):
        raise ValueError("news classifications must have unique article IDs")
    for item in results:
        # Re-run the immutable agent contract so ``model_copy`` cannot smuggle
        # a rechecksummed but internally inconsistent receipt into aggregation.
        NewsClassificationResult.model_validate(item.model_dump(mode="json"))
        if item.request.cutoff_at != cutoff or item.request.article.published_at >= cutoff:
            raise ValueError("news classification is not eligible before aggregation cutoff")
        if item.result_sha256 != item.content_checksum():
            raise ValueError("news classification result checksum does not match")
        if item.request.request_sha256 != item.request.content_checksum():
            raise ValueError("news classification request checksum does not match")
    _validate_duplicate_receipts(results)


def _validate_duplicate_receipts(results: tuple[NewsClassificationResult, ...]) -> None:
    """Require a single complete clustering receipt for submitted members.

    A result may legitimately have no duplicates, but it may not be combined
    with another submitted result whose immutable full clustering receipt says
    it is a member of a syndicated cluster.  That would otherwise allow the
    same wire story to be counted once as an unclustered item and again as a
    representative/non-representative under a different receipt.
    """

    by_id = {item.request.article.provider_article_id: item for item in results}
    receipts_by_article: dict[str, set[str]] = {}
    for result in results:
        clustering = result.request.duplicate_clustering
        if clustering is None:
            continue
        checksum = clustering.clustering_sha256
        if checksum is None:  # defensive; the clustering model normally fills this.
            raise ValueError("duplicate clustering requires a checksum receipt")
        for candidate in clustering.input_articles:
            if candidate.article_id in by_id:
                receipts_by_article.setdefault(candidate.article_id, set()).add(checksum)
    for article_id, checksums in receipts_by_article.items():
        if len(checksums) != 1:
            raise ValueError("submitted duplicate members disagree on clustering receipt")
        own = by_id[article_id].request.duplicate_clustering
        if own is None or own.clustering_sha256 not in checksums:
            raise ValueError("submitted duplicate member lacks the complete clustering receipt")


def _recency(age: timedelta, policy: NewsSignalPolicy) -> Decimal:
    hours = Decimal(age.total_seconds()) / Decimal("3600")
    if hours <= 24: return policy.recency_24h
    if hours <= 72: return policy.recency_72h
    if hours <= 168: return policy.recency_7d
    if hours <= 720: return policy.recency_30d
    return policy.recency_older


def _relevance(value: NewsRelevance, policy: NewsSignalPolicy) -> Decimal:
    return {NewsRelevance.HIGH: policy.relevance_high, NewsRelevance.MEDIUM: policy.relevance_medium, NewsRelevance.LOW: policy.relevance_low}.get(value, _ZERO)


def _materiality(value: NewsMateriality, policy: NewsSignalPolicy) -> Decimal:
    return {NewsMateriality.HIGH: policy.materiality_high, NewsMateriality.MEDIUM: policy.materiality_medium, NewsMateriality.LOW: policy.materiality_low}.get(value, _ZERO)


def _tone_value(value: NewsTone | None) -> Decimal:
    return {NewsTone.POSITIVE: _ONE, NewsTone.NEGATIVE: Decimal("-1"), NewsTone.MIXED: _ZERO, NewsTone.NEUTRAL: _ZERO}.get(value, _ZERO)


def _weighted_tone(items: tuple[NewsSignalItem, ...], accessor) -> Decimal | None:
    return _weighted_decimal(items, lambda item: _tone_value(accessor(item)))


def _weighted_decimal(items: tuple[NewsSignalItem, ...], accessor) -> Decimal | None:
    denominator = sum((item.weight for item in items), _ZERO)
    return None if not denominator else sum((item.weight * (accessor(item) or _ZERO) for item in items), _ZERO) / denominator


def _contradiction(items: tuple[NewsSignalItem, ...], material_threshold: Decimal) -> Decimal:
    """Opposing material content conflicts only when it addresses one topic."""

    by_topic: dict[NewsTopic, dict[NewsTone, Decimal]] = {}
    for item in items:
        if (
            item.content_tone not in {NewsTone.POSITIVE, NewsTone.NEGATIVE}
            or item.materiality_factor < material_threshold
            or not item.topics
        ):
            continue
        split = item.weight / Decimal(len(item.topics))
        for topic in item.topics:
            values = by_topic.setdefault(topic, {NewsTone.POSITIVE: _ZERO, NewsTone.NEGATIVE: _ZERO})
            values[item.content_tone] += split
    overlap = sum((min(values[NewsTone.POSITIVE], values[NewsTone.NEGATIVE]) for values in by_topic.values()), _ZERO)
    directional = sum((values[NewsTone.POSITIVE] + values[NewsTone.NEGATIVE] for values in by_topic.values()), _ZERO)
    return _ZERO if not directional else (Decimal("2") * overlap) / directional


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("news signal cutoff_at must use UTC")
    return value.astimezone(timezone.utc)


__all__ = ["NEWS_SIGNAL_CALCULATION_VERSION", "NewsCoverageQuality", "NewsSignal", "NewsSignalInputReceipt", "NewsSignalItem", "NewsSignalPolicy", "NewsTopicDistribution", "aggregate_news_signal"]
