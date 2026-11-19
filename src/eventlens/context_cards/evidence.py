"""Fail-closed evidence classification and analytical-use policy for v4 cards.

An evidence class is a provenance statement, not a quality score.  This module
keeps that statement attached to a compact receipt and makes every analytical
use pass through a deterministic policy check.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Confidence = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class EvidenceClass(StrEnum):
    """How a receipt's claim relates to its underlying source material."""

    SOURCE_NATIVE = "source_native"
    RECONSTRUCTED = "reconstructed"
    DERIVED = "derived"
    INFERRED = "inferred"
    ATTRIBUTED = "attributed"


class AnalyticalUse(StrEnum):
    """Material ways evidence can influence an EventLens analysis."""

    DESCRIPTIVE_CONTEXT = "descriptive_context"
    SURPRISE_COMPUTATION = "surprise_computation"
    BACKTEST_FEATURE = "backtest_feature"
    BACKTEST_LABEL = "backtest_label"
    AUTONOMOUS_DECISION = "autonomous_decision"
    SUPPORT_INTERPRETATION = "support_interpretation"
    PROVE_CAUSATION = "prove_causation"
    REPRESENT_AS_SOURCE_NATIVE = "represent_as_source_native"


class AnalyticalPermission(StrEnum):
    """A policy result before or after receipt-level conditions are checked."""

    ALLOWED = "allowed"
    CONDITIONAL = "conditional"
    PROHIBITED = "prohibited"
    # A convenient spelling for callers accustomed to access-control policies.
    DENIED = "prohibited"


class NewsRightsStatus(StrEnum):
    """How article-level text rights were handled for news evidence."""

    CLOSED_METADATA_ONLY = "closed_metadata_only"
    PROVIDER_TERMS_REVIEW_PENDING = "provider_terms_review_pending"
    METADATA_ONLY_INTERNAL = "metadata_only_internal"
    BLOCKED = "blocked"
    METADATA_ONLY_CLOSED_TEXT_EXCLUDED = "closed_metadata_only"


class NewsEvidencePhase(StrEnum):
    """Where a news item sits relative to the earnings-event workflow."""

    PRE_EVENT = "pre_event"
    EVENT_WINDOW = "event_window"
    POST_EVENT = "post_event"
    REFERENCE = "reference"


class NewsSignalBasis(StrEnum):
    """Whether relevance/sentiment values are provider-native or absent."""

    PROVIDER_NATIVE = "provider_native"
    NOT_PROVIDED = "not_provided"


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class EvidenceReceipt(_EvidenceModel):
    """Compact, typed evidence provenance safe to embed in a context card.

    ``source_ref`` and ``temporal_context_ref`` are stable pointers.  Provider
    response bodies deliberately have no field here.  Derived and inferred
    receipts instead point to other receipts in the card's registry.
    """

    id: NonEmptyStr
    evidence_class: EvidenceClass
    claim: NonEmptyStr
    source_ref: NonEmptyStr | None = None
    temporal_context_ref: NonEmptyStr | None = None
    input_receipt_ids: tuple[NonEmptyStr, ...] = ()
    operands_validated: bool = False
    confidence: Confidence | None = None
    claims_causation: bool = False

    @property
    def evidence_id(self) -> str:
        """Compatibility/readability alias for the receipt's stable id."""

        return self.id

    @model_validator(mode="after")
    def class_requirements_are_explicit(self) -> "EvidenceReceipt":
        if len(set(self.input_receipt_ids)) != len(self.input_receipt_ids):
            raise ValueError("input_receipt_ids cannot contain duplicates")
        if self.id in self.input_receipt_ids:
            raise ValueError("an evidence receipt cannot depend on itself")

        if self.evidence_class in {
            EvidenceClass.SOURCE_NATIVE,
            EvidenceClass.RECONSTRUCTED,
            EvidenceClass.ATTRIBUTED,
        } and self.source_ref is None:
            raise ValueError(f"{self.evidence_class.value} evidence requires source_ref")

        if self.evidence_class is EvidenceClass.DERIVED:
            if not self.input_receipt_ids:
                raise ValueError("derived evidence requires operand receipt ids")
            if not self.operands_validated:
                raise ValueError("derived evidence requires validated operand receipts")
        elif self.operands_validated:
            raise ValueError("operands_validated applies only to derived evidence")

        if self.evidence_class is EvidenceClass.INFERRED:
            if self.confidence is None:
                raise ValueError("inferred evidence requires confidence")
            if not self.input_receipt_ids:
                raise ValueError("inferred evidence requires traceable input receipts")

        if self.evidence_class is EvidenceClass.ATTRIBUTED and self.claims_causation:
            raise ValueError("attributed evidence cannot prove causation")
        return self


class NewsEvidenceReceipt(EvidenceReceipt):
    """Rights-aware metadata-only news receipt.

    News APIs commonly return copyrighted article summaries or descriptions
    alongside metadata.  This receipt is deliberately closed to a small
    metadata allow-list; it rejects raw provider body fields and does not carry
    summaries, snippets, descriptions, article bodies, image payloads, or
    arbitrary provider metadata maps.
    """

    provider_name: NonEmptyStr
    provider_article_id: NonEmptyStr
    ticker: NonEmptyStr
    query_start: date
    query_end: date
    cutoff_at: datetime
    evidence_phase: NewsEvidencePhase
    item_sha: Sha256
    response_sha: Sha256
    headline: NonEmptyStr
    url: NonEmptyStr
    published_at: datetime
    retrieved_at: datetime
    relevance_value: Confidence | None = None
    sentiment_value: Decimal | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    sentiment_label: NonEmptyStr | None = None
    source_name: NonEmptyStr | None = None
    source_domain: NonEmptyStr | None = None
    category: NonEmptyStr | None = None
    related_tickers: tuple[NonEmptyStr, ...] = ()
    relevance_basis: NewsSignalBasis = NewsSignalBasis.NOT_PROVIDED
    sentiment_basis: NewsSignalBasis = NewsSignalBasis.NOT_PROVIDED
    rights_status: NewsRightsStatus = NewsRightsStatus.PROVIDER_TERMS_REVIEW_PENDING
    excluded_text_fields: tuple[NonEmptyStr, ...] = (
        "summary",
        "description",
        "body",
        "content",
        "snippet",
        "excerpt",
    )

    @field_validator("ticker", "provider_name", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip().upper() if value.strip().isalpha() else value.strip()

    @field_validator("related_tickers", mode="before")
    @classmethod
    def normalize_related_tickers(cls, value: object) -> object:
        if value is None:
            return ()
        if isinstance(value, str):
            items = (value,)
        else:
            items = tuple(value)  # type: ignore[arg-type]
        return tuple(str(item).strip().upper() for item in items if str(item).strip())

    @field_validator("published_at")
    @classmethod
    def published_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("cutoff_at")
    @classmethod
    def cutoff_at_is_strictly_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cutoff_at must be timezone-aware UTC")
        if value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("cutoff_at must use UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def receipt_is_metadata_only(self) -> "NewsEvidenceReceipt":
        if self.evidence_class is not EvidenceClass.RECONSTRUCTED:
            raise ValueError("news evidence receipts must be reconstructed metadata receipts")
        if self.rights_status in {NewsRightsStatus.BLOCKED}:
            raise ValueError("blocked news evidence cannot be used as a receipt")
        if self.query_end < self.query_start:
            raise ValueError("news evidence query_end must be on or after query_start")
        if not self.query_start <= self.published_at.date() <= self.query_end:
            raise ValueError("news evidence published_at must fall within query bounds")
        if self.cutoff_at is not None:
            if self.published_at >= self.cutoff_at:
                raise ValueError("news evidence published_at must be strictly before cutoff_at")
        if len(set(self.related_tickers)) != len(self.related_tickers):
            raise ValueError("related_tickers cannot contain duplicates")
        if self.relevance_value is not None and self.relevance_basis is NewsSignalBasis.NOT_PROVIDED:
            raise ValueError("relevance_value requires provider_native relevance_basis")
        if self.sentiment_value is not None and self.sentiment_basis is NewsSignalBasis.NOT_PROVIDED:
            raise ValueError("sentiment_value requires provider_native sentiment_basis")
        if self.sentiment_label is not None and self.sentiment_basis is NewsSignalBasis.NOT_PROVIDED:
            raise ValueError("sentiment_label requires provider_native sentiment_basis")
        if "\n" in self.headline or len(self.headline) > 300:
            raise ValueError("headline must be compact metadata, not article text")
        if any(
            field not in {"summary", "description", "body", "content", "snippet", "excerpt"}
            for field in self.excluded_text_fields
        ):
            raise ValueError("excluded_text_fields must name closed article-text fields")
        return self


class EvidencePolicy(_EvidenceModel):
    """Explicit permission matrix; absent classes and uses are prohibited."""

    permissions: dict[
        EvidenceClass, dict[AnalyticalUse, AnalyticalPermission]
    ] = Field(default_factory=dict)

    def permission_for(
        self,
        evidence: EvidenceClass | EvidenceReceipt,
        use: AnalyticalUse,
        *,
        registry: object | None = None,
    ) -> AnalyticalPermission:
        evidence_class = (
            evidence.evidence_class if isinstance(evidence, EvidenceReceipt) else evidence
        )
        permission = self.permissions.get(evidence_class, {}).get(
            use, AnalyticalPermission.PROHIBITED
        )
        if permission is not AnalyticalPermission.CONDITIONAL:
            return permission
        if not isinstance(evidence, EvidenceReceipt):
            return AnalyticalPermission.CONDITIONAL
        return (
            AnalyticalPermission.ALLOWED
            if _conditions_satisfied(evidence, use=use, policy=self, registry=registry)
            else AnalyticalPermission.PROHIBITED
        )


_SOURCE_NATIVE = {
    AnalyticalUse.DESCRIPTIVE_CONTEXT: AnalyticalPermission.ALLOWED,
    AnalyticalUse.SURPRISE_COMPUTATION: AnalyticalPermission.ALLOWED,
    AnalyticalUse.BACKTEST_FEATURE: AnalyticalPermission.ALLOWED,
    AnalyticalUse.BACKTEST_LABEL: AnalyticalPermission.ALLOWED,
    AnalyticalUse.AUTONOMOUS_DECISION: AnalyticalPermission.ALLOWED,
    AnalyticalUse.SUPPORT_INTERPRETATION: AnalyticalPermission.ALLOWED,
    AnalyticalUse.PROVE_CAUSATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.REPRESENT_AS_SOURCE_NATIVE: AnalyticalPermission.ALLOWED,
}

_RECONSTRUCTED = {
    AnalyticalUse.DESCRIPTIVE_CONTEXT: AnalyticalPermission.ALLOWED,
    AnalyticalUse.SURPRISE_COMPUTATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.BACKTEST_FEATURE: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.BACKTEST_LABEL: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.AUTONOMOUS_DECISION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.SUPPORT_INTERPRETATION: AnalyticalPermission.ALLOWED,
    AnalyticalUse.PROVE_CAUSATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.REPRESENT_AS_SOURCE_NATIVE: AnalyticalPermission.PROHIBITED,
}

_DERIVED = {
    AnalyticalUse.DESCRIPTIVE_CONTEXT: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.SURPRISE_COMPUTATION: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.BACKTEST_FEATURE: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.BACKTEST_LABEL: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.AUTONOMOUS_DECISION: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.SUPPORT_INTERPRETATION: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.PROVE_CAUSATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.REPRESENT_AS_SOURCE_NATIVE: AnalyticalPermission.PROHIBITED,
}

_INFERRED = {
    AnalyticalUse.DESCRIPTIVE_CONTEXT: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.SURPRISE_COMPUTATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.BACKTEST_FEATURE: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.BACKTEST_LABEL: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.AUTONOMOUS_DECISION: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.SUPPORT_INTERPRETATION: AnalyticalPermission.CONDITIONAL,
    AnalyticalUse.PROVE_CAUSATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.REPRESENT_AS_SOURCE_NATIVE: AnalyticalPermission.PROHIBITED,
}

_ATTRIBUTED = {
    AnalyticalUse.DESCRIPTIVE_CONTEXT: AnalyticalPermission.ALLOWED,
    AnalyticalUse.SURPRISE_COMPUTATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.BACKTEST_FEATURE: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.BACKTEST_LABEL: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.AUTONOMOUS_DECISION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.SUPPORT_INTERPRETATION: AnalyticalPermission.ALLOWED,
    AnalyticalUse.PROVE_CAUSATION: AnalyticalPermission.PROHIBITED,
    AnalyticalUse.REPRESENT_AS_SOURCE_NATIVE: AnalyticalPermission.PROHIBITED,
}


DEFAULT_EVIDENCE_POLICY = EvidencePolicy(
    permissions={
        EvidenceClass.SOURCE_NATIVE: _SOURCE_NATIVE,
        EvidenceClass.RECONSTRUCTED: _RECONSTRUCTED,
        EvidenceClass.DERIVED: _DERIVED,
        EvidenceClass.INFERRED: _INFERRED,
        EvidenceClass.ATTRIBUTED: _ATTRIBUTED,
    }
)


def permission_for(
    evidence: EvidenceClass | EvidenceReceipt,
    use: AnalyticalUse,
    *,
    policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
    registry: object | None = None,
) -> AnalyticalPermission:
    """Return the deterministic permission, failing closed without a policy."""

    if policy is None:
        return AnalyticalPermission.PROHIBITED
    if not isinstance(policy, EvidencePolicy):
        raise TypeError("policy must be an EvidencePolicy or None")
    try:
        normalized_use = AnalyticalUse(use)
        normalized_evidence = (
            evidence
            if isinstance(evidence, EvidenceReceipt)
            else EvidenceClass(evidence)
        )
    except (TypeError, ValueError):
        return AnalyticalPermission.PROHIBITED
    return policy.permission_for(normalized_evidence, normalized_use, registry=registry)


def require_permission(
    evidence: EvidenceClass | EvidenceReceipt,
    use: AnalyticalUse,
    *,
    policy: EvidencePolicy | None = DEFAULT_EVIDENCE_POLICY,
    registry: object | None = None,
) -> EvidenceClass | EvidenceReceipt:
    """Return ``evidence`` when allowed, otherwise raise ``PermissionError``."""

    permission = permission_for(evidence, use, policy=policy, registry=registry)
    if permission is not AnalyticalPermission.ALLOWED:
        evidence_class = (
            evidence.evidence_class if isinstance(evidence, EvidenceReceipt) else evidence
        )
        class_label = getattr(evidence_class, "value", str(evidence_class))
        use_label = getattr(use, "value", str(use))
        raise PermissionError(
            f"{class_label} evidence is not permitted for {use_label}: "
            f"{permission.value}"
        )
    return evidence


def _conditions_satisfied(
    receipt: EvidenceReceipt,
    *,
    use: AnalyticalUse,
    policy: EvidencePolicy,
    registry: object | None,
) -> bool:
    if receipt.evidence_class is EvidenceClass.DERIVED:
        if not receipt.operands_validated or not receipt.input_receipt_ids:
            return False
        registry_receipts = _registry_receipts(registry)
        if registry_receipts is None:
            return False
        operands = tuple(
            registry_receipts.get(receipt_id) for receipt_id in receipt.input_receipt_ids
        )
        if any(operand is None for operand in operands):
            return False
        # A derived receipt cannot launder a restricted operand into a stronger
        # use merely by declaring that its arithmetic was checked.
        return all(
            policy.permission_for(operand, use, registry=registry)
            is AnalyticalPermission.ALLOWED
            for operand in operands
        )
    if receipt.evidence_class is EvidenceClass.INFERRED:
        if receipt.confidence is None or not receipt.input_receipt_ids:
            return False
        registry_receipts = _registry_receipts(registry)
        if registry_receipts is None:
            return False
        inputs = tuple(
            registry_receipts.get(receipt_id) for receipt_id in receipt.input_receipt_ids
        )
        if any(input_receipt is None for input_receipt in inputs):
            return False
        return all(
            policy.permission_for(input_receipt, use, registry=registry)
            is AnalyticalPermission.ALLOWED
            for input_receipt in inputs
        )
    return False


def _registry_receipts(registry: object | None) -> Mapping[str, EvidenceReceipt] | None:
    """Read a registry through its public lookup surface without importing models."""

    if registry is None:
        return None
    by_id = getattr(registry, "receipts_by_id", None)
    return by_id if isinstance(by_id, Mapping) else None


__all__ = [
    "DEFAULT_EVIDENCE_POLICY",
    "AnalyticalPermission",
    "AnalyticalUse",
    "EvidenceClass",
    "EvidencePolicy",
    "EvidenceReceipt",
    "NewsEvidencePhase",
    "NewsEvidenceReceipt",
    "NewsRightsStatus",
    "NewsSignalBasis",
    "permission_for",
    "require_permission",
]
