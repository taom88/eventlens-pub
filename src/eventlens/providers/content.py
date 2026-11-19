"""Rights-aware, bounded article-content retrieval.

This module deliberately is not a crawler.  Discovery has already happened in
``NewsArticleMetadata``; this boundary may make one direct request only when
the caller has recorded that access and retention are allowed.  In particular,
it does not authenticate, follow redirects, work around a paywall, or attempt
to override robots/access decisions.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._common import strict_utc_cutoff
from .models import NewsArticleMetadata, NewsSummaryStatus


class ContentSourceKind(StrEnum):
    SEC = "sec"
    ISSUER = "issuer"
    PROVIDER = "provider"
    THIRD_PARTY = "third_party"


class ContentFetchStatus(StrEnum):
    """The evidence quality exposed to downstream consumers."""

    CONTENT_AVAILABLE = "content_available"
    SUMMARY_ONLY = "summary_only"
    HEADLINE_ONLY = "headline_only"


class ContentFetchReason(StrEnum):
    CONTENT_PARSED = "content_parsed"
    PROVIDER_SUMMARY_ONLY = "provider_summary_only"
    ACCESS_NOT_PERMITTED = "access_not_permitted"
    ROBOTS_DISALLOWED = "robots_disallowed"
    PAYWALL_OR_LOGIN = "paywall_or_login"
    ACCESS_DENIED = "access_denied"
    ACCESS_FAILURE = "access_failure"
    POST_CUTOFF = "post_cutoff"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    RESPONSE_TOO_LARGE = "response_too_large"
    BODY_EXCEEDS_BOUND = "body_exceeds_bound"
    NO_MAIN_CONTENT = "no_main_content"
    REDIRECT_REFUSED = "redirect_refused"
    URL_NOT_ALLOWED = "url_not_allowed"
    NO_PERMITTED_EXCERPTS = "no_permitted_excerpts"


class ContentRetentionPolicy(StrEnum):
    FULL_BODY = "full_body"
    BOUNDED_EXCERPTS = "bounded_excerpts"
    METADATA_ONLY = "metadata_only"


class ContentRightsStatus(StrEnum):
    """What may be persisted, independent of whether a request succeeded."""

    FULL_BODY_PERMITTED = "full_body_permitted"
    EXCERPTS_PERMITTED = "excerpts_permitted"
    METADATA_ONLY = "metadata_only"


class ContentAccessStatus(StrEnum):
    """The direct-request outcome, independent of rights and evidence quality."""

    NOT_ATTEMPTED = "not_attempted"
    FETCHED = "fetched"
    ACCESS_NOT_PERMITTED = "access_not_permitted"
    ROBOTS_DISALLOWED = "robots_disallowed"
    PAYWALL_OR_LOGIN = "paywall_or_login"
    ACCESS_DENIED = "access_denied"
    ACCESS_FAILURE = "access_failure"
    REDIRECT_REFUSED = "redirect_refused"
    POST_CUTOFF = "post_cutoff"
    TEMPORAL_UNVERIFIED = "temporal_unverified"
    RESPONSE_REJECTED = "response_rejected"


class ContentTemporalStatus(StrEnum):
    VERIFIED_BEFORE_CUTOFF = "verified_before_cutoff"
    POST_CUTOFF = "post_cutoff"
    MUTABLE_PAGE_TEMPORAL_UNVERIFIED = "mutable_page_temporal_unverified"


class ContentTemporalBasis(StrEnum):
    SOURCE_CONTENT_TIMESTAMP = "source_content_timestamp"
    RETRIEVED_BEFORE_CUTOFF = "retrieved_before_cutoff"
    NONE = "none"


CONTENT_PARSER_VERSION = "mvp-305.2"
CONTENT_EVIDENCE_SCHEMA_VERSION = "eventlens.content-evidence.v1"


class ContentFetchPolicy(BaseModel):
    """Explicit access, retention, and output bounds for one source class.

    ``access_permitted`` and ``robots_permitted`` must be determined outside
    this module (for example from source terms and a robots check).  A false
    value is fail-closed and no network request is attempted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: ContentSourceKind
    grant_id: str = Field(min_length=1, max_length=128)
    grant_version: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=500)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=20)
    access_permitted: bool = False
    robots_permitted: bool = False
    retention_policy: ContentRetentionPolicy = ContentRetentionPolicy.METADATA_ONLY
    max_response_bytes: int = Field(default=1_000_000, ge=1, le=5_000_000)
    max_body_characters: int = Field(default=250_000, ge=1, le=500_000)
    max_paragraphs: int = Field(default=500, ge=1, le=2_000)
    max_paragraph_characters: int = Field(default=8_000, ge=1, le=20_000)
    max_excerpt_count: int = Field(default=3, ge=0, le=20)
    max_excerpt_characters: int = Field(default=1_200, ge=1, le=5_000)

    @model_validator(mode="after")
    def retention_has_consistent_bounds(self) -> ContentFetchPolicy:
        if self.retention_policy is ContentRetentionPolicy.BOUNDED_EXCERPTS and self.max_excerpt_count == 0:
            raise ValueError("bounded_excerpts requires max_excerpt_count greater than zero")
        normalized_hosts = tuple(sorted({host.casefold().strip().rstrip(".") for host in self.allowed_hosts}))
        if len(normalized_hosts) != len(self.allowed_hosts) or any(not _is_public_hostname(host) for host in normalized_hosts):
            raise ValueError("allowed_hosts must contain unique public hostnames only")
        object.__setattr__(self, "allowed_hosts", normalized_hosts)
        return self

    def canonical_sha256(self) -> str:
        return _sha256(self.model_dump(mode="json"))

    @classmethod
    def issuer_or_sec(
        cls,
        *,
        source_kind: ContentSourceKind = ContentSourceKind.ISSUER,
        grant_id: str,
        grant_version: str,
        source_ref: str,
        allowed_hosts: tuple[str, ...],
        robots_permitted: bool,
    ) -> ContentFetchPolicy:
        if source_kind not in {ContentSourceKind.ISSUER, ContentSourceKind.SEC}:
            raise ValueError("issuer_or_sec policy only supports issuer or sec sources")
        return cls(
            source_kind=source_kind,
            grant_id=grant_id,
            grant_version=grant_version,
            source_ref=source_ref,
            allowed_hosts=allowed_hosts,
            access_permitted=True,
            robots_permitted=robots_permitted,
            retention_policy=ContentRetentionPolicy.FULL_BODY,
        )


class ContentParagraph(BaseModel):
    """A stable paragraph receipt; text is omitted when not retainable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paragraph_id: str = Field(min_length=1, max_length=180)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ordinal: int = Field(ge=1)
    text: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def text_matches_hash_when_retained(self) -> ContentParagraph:
        if self.text is not None and _text_sha256(self.text) != self.text_sha256:
            raise ValueError("paragraph text_sha256 does not match retained text")
        return self


class ContentExcerpt(BaseModel):
    """One rights-permitted bounded excerpt copied from a paragraph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paragraph_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=5_000)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def excerpt_matches_hash(self) -> ContentExcerpt:
        if _text_sha256(self.text) != self.text_sha256:
            raise ValueError("excerpt text_sha256 does not match text")
        return self


class ContentEvidence(BaseModel):
    """Persistable, rights-safe content evidence for one discovered article."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_article_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    final_url: str | None = None
    source_host: str = Field(min_length=1)
    published_at: datetime
    cutoff_at: datetime
    fetched_at: datetime | None = None
    content_available_at: datetime | None = None
    temporal_basis: ContentTemporalBasis = ContentTemporalBasis.NONE
    article_metadata_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    discovery_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_receipt: ContentFetchPolicy
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    grant_id: str = Field(min_length=1, max_length=128)
    grant_version: str = Field(min_length=1, max_length=64)
    source_kind: ContentSourceKind
    source_ref: str = Field(min_length=1, max_length=500)
    schema_version: Literal[CONTENT_EVIDENCE_SCHEMA_VERSION] = CONTENT_EVIDENCE_SCHEMA_VERSION
    parser_version: str = Field(default=CONTENT_PARSER_VERSION, min_length=1)
    status: ContentFetchStatus
    reason: ContentFetchReason
    access_status: ContentAccessStatus
    rights_status: ContentRightsStatus
    temporal_status: ContentTemporalStatus
    retention_policy: ContentRetentionPolicy
    response_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    response_content_type: str | None = Field(default=None, max_length=200)
    response_byte_count: int | None = Field(default=None, ge=0)
    excerpt_count_limit: int = Field(ge=0, le=20)
    excerpt_character_limit: int = Field(ge=1, le=5_000)
    paragraphs: tuple[ContentParagraph, ...] = ()
    excerpts: tuple[ContentExcerpt, ...] = ()
    full_body: str | None = Field(default=None, max_length=500_000)
    quality_warning: str | None = Field(default=None, max_length=500)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("published_at", "cutoff_at", "fetched_at", "content_available_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("content timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def enforce_rights_safe_retention(self) -> ContentEvidence:
        if _source_host(self.source_url) != self.source_host:
            raise ValueError("source_host must match source_url")
        if self.final_url is not None and self.final_url != self.source_url:
            raise ValueError("final_url must equal source_url because redirects are refused")
        if self.policy_sha256 != self.policy_receipt.canonical_sha256():
            raise ValueError("policy_sha256 does not match embedded policy receipt")
        if self.source_kind is not self.policy_receipt.source_kind or self.source_ref != self.policy_receipt.source_ref:
            raise ValueError("source authorization fields must match embedded policy receipt")
        if self.grant_id != self.policy_receipt.grant_id or self.grant_version != self.policy_receipt.grant_version:
            raise ValueError("grant fields must match embedded policy receipt")
        if self.retention_policy is not self.policy_receipt.retention_policy:
            raise ValueError("retention policy must match embedded policy receipt")
        if (
            self.excerpt_count_limit != self.policy_receipt.max_excerpt_count
            or self.excerpt_character_limit != self.policy_receipt.max_excerpt_characters
        ):
            raise ValueError("excerpt bounds must match embedded policy receipt")
        if (
            self.status is ContentFetchStatus.CONTENT_AVAILABLE
            and self.source_host not in self.policy_receipt.allowed_hosts
        ):
            raise ValueError("source host is outside the embedded policy receipt")
        paragraph_ids = {paragraph.paragraph_id for paragraph in self.paragraphs}
        if len(paragraph_ids) != len(self.paragraphs):
            raise ValueError("content paragraph IDs must be unique")
        if [paragraph.ordinal for paragraph in self.paragraphs] != list(range(1, len(self.paragraphs) + 1)):
            raise ValueError("content paragraph ordinals must be ordered and contiguous")
        if any(excerpt.paragraph_id not in paragraph_ids for excerpt in self.excerpts):
            raise ValueError("content excerpts must refer to known paragraphs")
        if len(self.excerpts) > self.excerpt_count_limit or any(len(excerpt.text) > self.excerpt_character_limit for excerpt in self.excerpts):
            raise ValueError("content excerpts exceed persisted policy bounds")
        if self.retention_policy is ContentRetentionPolicy.FULL_BODY:
            if self.rights_status is not ContentRightsStatus.FULL_BODY_PERMITTED:
                raise ValueError("full_body retention requires full_body_permitted rights")
            if self.status is ContentFetchStatus.CONTENT_AVAILABLE and not self.full_body:
                raise ValueError("full_body retention requires body content when content is available")
            if self.excerpts:
                raise ValueError("full_body retention must not duplicate excerpts")
            if self.full_body is not None and any(paragraph.text is None for paragraph in self.paragraphs):
                raise ValueError("full_body retention requires every paragraph text to be retained")
        elif self.full_body is not None:
            raise ValueError("full_body is allowed only under full_body retention")
        elif self.retention_policy is ContentRetentionPolicy.BOUNDED_EXCERPTS and self.rights_status is not ContentRightsStatus.EXCERPTS_PERMITTED:
            raise ValueError("bounded_excerpts retention requires excerpts_permitted rights")
        elif self.retention_policy is ContentRetentionPolicy.METADATA_ONLY and self.rights_status is not ContentRightsStatus.METADATA_ONLY:
            raise ValueError("metadata_only retention requires metadata_only rights")
        if self.retention_policy is ContentRetentionPolicy.METADATA_ONLY and self.excerpts:
            raise ValueError("metadata_only retention cannot store excerpts")
        if (
            self.retention_policy is ContentRetentionPolicy.BOUNDED_EXCERPTS
            and any(paragraph.text is not None for paragraph in self.paragraphs)
        ):
            raise ValueError(
                "bounded_excerpts retention cannot store full paragraph text"
            )
        if self.status is not ContentFetchStatus.CONTENT_AVAILABLE and (self.excerpts or self.full_body):
            raise ValueError("non-content evidence cannot retain readable article text")
        if self.status is not ContentFetchStatus.CONTENT_AVAILABLE and any(paragraph.text is not None for paragraph in self.paragraphs):
            raise ValueError("non-content evidence cannot retain full paragraph text")
        if self.status is ContentFetchStatus.CONTENT_AVAILABLE and not (self.full_body or self.excerpts):
            raise ValueError("content_available evidence requires permitted readable content")
        if self.status is ContentFetchStatus.CONTENT_AVAILABLE and self.temporal_status is not ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF:
            raise ValueError("content_available evidence requires verified pre-cutoff content availability")
        if self.status is ContentFetchStatus.CONTENT_AVAILABLE and not (
            self.reason is ContentFetchReason.CONTENT_PARSED
            and self.access_status is ContentAccessStatus.FETCHED
        ):
            raise ValueError("content_available evidence requires a fetched parsed-content outcome")
        if self.reason is ContentFetchReason.POST_CUTOFF and self.temporal_status is not ContentTemporalStatus.POST_CUTOFF:
            raise ValueError("post_cutoff reason requires post_cutoff temporal status")
        if self.published_at >= self.cutoff_at and self.temporal_status is not ContentTemporalStatus.POST_CUTOFF:
            raise ValueError("at-or-after-cutoff article must be marked post_cutoff")
        if self.full_body is not None and self.full_body != "\n\n".join(item.text or "" for item in self.paragraphs):
            raise ValueError("full_body must match retained paragraph text")
        if self.temporal_basis is ContentTemporalBasis.SOURCE_CONTENT_TIMESTAMP and self.content_available_at is None:
            raise ValueError("source timestamp temporal basis requires content_available_at")
        if self.content_available_at is not None and self.temporal_basis is not ContentTemporalBasis.SOURCE_CONTENT_TIMESTAMP:
            raise ValueError("content_available_at is authoritative and requires source_content_timestamp basis")
        if self.temporal_basis is ContentTemporalBasis.SOURCE_CONTENT_TIMESTAMP:
            if self.content_available_at is None:
                raise ValueError("source timestamp basis requires content_available_at")
            if self.content_available_at < self.cutoff_at:
                if self.temporal_status is not ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF:
                    raise ValueError("pre-cutoff source timestamp requires verified temporal status")
            elif self.temporal_status is not ContentTemporalStatus.POST_CUTOFF:
                raise ValueError("at-or-after-cutoff source timestamp requires post_cutoff temporal status")
        if self.temporal_basis is ContentTemporalBasis.RETRIEVED_BEFORE_CUTOFF:
            if self.fetched_at is None or self.fetched_at >= self.cutoff_at:
                raise ValueError("retrieved timestamp basis requires fetched_at strictly before cutoff")
            if self.content_available_at is not None or self.temporal_status is not ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF:
                raise ValueError("retrieved timestamp basis cannot override source availability or temporal status")
        if self.temporal_status is ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF and self.temporal_basis is ContentTemporalBasis.NONE:
            raise ValueError("verified temporal status requires a temporal basis")
        if self.temporal_status is ContentTemporalStatus.POST_CUTOFF:
            if self.reason is not ContentFetchReason.POST_CUTOFF or self.access_status is not ContentAccessStatus.POST_CUTOFF:
                raise ValueError("post_cutoff temporal status requires post_cutoff reason and access status")
            if self.temporal_basis is ContentTemporalBasis.RETRIEVED_BEFORE_CUTOFF:
                raise ValueError("post_cutoff temporal status cannot use retrieved_before_cutoff basis")
        if self.reason is ContentFetchReason.POST_CUTOFF and (
            self.temporal_status is not ContentTemporalStatus.POST_CUTOFF
            or self.access_status is not ContentAccessStatus.POST_CUTOFF
        ):
            raise ValueError("post_cutoff reason requires post_cutoff temporal and access status")
        receipt_fields = (self.fetched_at, self.response_content_type, self.response_byte_count, self.final_url)
        if self.response_sha256 is not None and any(value is None for value in receipt_fields):
            raise ValueError("response hash requires a complete response receipt")
        provider_summary_receipt = (
            self.source_kind is ContentSourceKind.PROVIDER
            and self.status is ContentFetchStatus.SUMMARY_ONLY
            and self.temporal_basis is ContentTemporalBasis.RETRIEVED_BEFORE_CUTOFF
            and self.fetched_at is not None
            and all(
                value is None
                for value in (
                    self.response_content_type,
                    self.response_byte_count,
                    self.final_url,
                )
            )
        )
        if (
            self.response_sha256 is None
            and any(value is not None for value in receipt_fields)
            and not provider_summary_receipt
        ):
            raise ValueError("response receipt fields require a response hash")
        expected = self.content_checksum()
        if self.evidence_sha256 is None:
            object.__setattr__(self, "evidence_sha256", expected)
        elif self.evidence_sha256 != expected:
            raise ValueError("evidence_sha256 does not match content evidence")
        return self

    def content_checksum(self) -> str:
        return _sha256(self.model_dump(mode="json", exclude={"evidence_sha256"}))


@dataclass(frozen=True)
class ContentHttpResponse:
    """Minimal, injectable HTTP response used by the direct-fetch boundary."""

    status_code: int
    headers: dict[str, str]
    body: bytes
    retrieved_at: datetime
    # An immutable source timestamp may be supplied by a source-specific
    # adapter.  It is distinct from retrieval time and is never guessed.
    content_available_at: datetime | None = None
    final_url: str | None = None


ContentExcerptSelector = Callable[[tuple[ContentParagraph, ...], ContentFetchPolicy], tuple[str, ...]]
HostnameResolver = Callable[[str], tuple[str, ...]]


class RightsAwareContentFetcher:
    """Fetch one already-discovered URL and return a bounded evidence record."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        resolver: HostnameResolver | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._resolver = resolver or _resolve_hostname

    def fetch(
        self,
        article: NewsArticleMetadata,
        *,
        cutoff_at: datetime,
        policy: ContentFetchPolicy,
        request: Callable[[str], ContentHttpResponse] | None = None,
        excerpt_selector: ContentExcerptSelector | None = None,
    ) -> ContentEvidence:
        cutoff = strict_utc_cutoff(cutoff_at)
        if article.published_at >= cutoff:
            return self._fallback(article, cutoff, policy, ContentFetchReason.POST_CUTOFF,
                                  access_status=ContentAccessStatus.POST_CUTOFF,
                                  temporal_status=ContentTemporalStatus.POST_CUTOFF)
        if (
            not policy.access_permitted
            and policy.source_kind is ContentSourceKind.PROVIDER
            and article.provider_summary is not None
            and article.retrieved_at is not None
            and article.retrieved_at < cutoff
        ):
            return self._fallback(
                article,
                cutoff,
                policy,
                ContentFetchReason.ACCESS_NOT_PERMITTED,
                access_status=ContentAccessStatus.NOT_ATTEMPTED,
                temporal_status=ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF,
                temporal_basis=ContentTemporalBasis.RETRIEVED_BEFORE_CUTOFF,
            )
        if not _url_allowed(article.url, policy.allowed_hosts):
            return self._fallback(article, cutoff, policy, ContentFetchReason.URL_NOT_ALLOWED,
                                  access_status=ContentAccessStatus.ACCESS_NOT_PERMITTED)
        if not policy.access_permitted:
            return self._fallback(article, cutoff, policy, ContentFetchReason.ACCESS_NOT_PERMITTED,
                                  access_status=ContentAccessStatus.ACCESS_NOT_PERMITTED)
        if not policy.robots_permitted:
            return self._fallback(article, cutoff, policy, ContentFetchReason.ROBOTS_DISALLOWED,
                                  access_status=ContentAccessStatus.ROBOTS_DISALLOWED)
        try:
            resolved_addresses = self._resolver(_source_host(article.url))
        except (OSError, ValueError):
            resolved_addresses = ()
        if not resolved_addresses or not all(
            _is_public_ip(address) for address in resolved_addresses
        ):
            return self._fallback(article, cutoff, policy, ContentFetchReason.URL_NOT_ALLOWED,
                                  access_status=ContentAccessStatus.ACCESS_NOT_PERMITTED)
        try:
            response = (
                request(article.url)
                if request is not None
                else self._request(
                    article.url,
                    resolved_addresses=resolved_addresses,
                    max_response_bytes=policy.max_response_bytes,
                )
            )
        except (httpx.HTTPError, OSError, ValueError):
            return self._fallback(article, cutoff, policy, ContentFetchReason.ACCESS_FAILURE,
                                  access_status=ContentAccessStatus.ACCESS_FAILURE)
        if response.retrieved_at.tzinfo is None or response.retrieved_at.utcoffset() is None:
            return self._fallback(article, cutoff, policy, ContentFetchReason.ACCESS_FAILURE,
                                  access_status=ContentAccessStatus.ACCESS_FAILURE)
        if response.final_url is not None and response.final_url != article.url:
            return self._fallback(article, cutoff, policy, ContentFetchReason.REDIRECT_REFUSED, response=response,
                                  access_status=ContentAccessStatus.REDIRECT_REFUSED)
        if 300 <= response.status_code < 400:
            return self._fallback(article, cutoff, policy, ContentFetchReason.REDIRECT_REFUSED, response=response,
                                  access_status=ContentAccessStatus.REDIRECT_REFUSED)
        if response.status_code in {401, 402} or _looks_like_paywall(response.body):
            return self._fallback(article, cutoff, policy, ContentFetchReason.PAYWALL_OR_LOGIN, response=response,
                                  access_status=ContentAccessStatus.PAYWALL_OR_LOGIN)
        if response.status_code in {403, 429}:
            return self._fallback(article, cutoff, policy, ContentFetchReason.ACCESS_DENIED, response=response,
                                  access_status=ContentAccessStatus.ACCESS_DENIED)
        if not 200 <= response.status_code < 300:
            return self._fallback(article, cutoff, policy, ContentFetchReason.ACCESS_FAILURE, response=response,
                                  access_status=ContentAccessStatus.ACCESS_FAILURE)
        if len(response.body) > policy.max_response_bytes:
            return self._fallback(article, cutoff, policy, ContentFetchReason.RESPONSE_TOO_LARGE, response=response,
                                  access_status=ContentAccessStatus.RESPONSE_REJECTED)
        content_type = next(
            (value for key, value in response.headers.items() if key.casefold() == "content-type"),
            "",
        ).casefold()
        if "html" not in content_type and "xhtml" not in content_type:
            return self._fallback(article, cutoff, policy, ContentFetchReason.UNSUPPORTED_CONTENT_TYPE, response=response,
                                  access_status=ContentAccessStatus.RESPONSE_REJECTED)
        try:
            decoded = response.body.decode(_charset_from_content_type(content_type), errors="replace")
        except LookupError:
            return self._fallback(article, cutoff, policy, ContentFetchReason.UNSUPPORTED_CONTENT_TYPE, response=response,
                                  access_status=ContentAccessStatus.RESPONSE_REJECTED)
        temporal, temporal_basis = _temporal_status(response, cutoff)
        if temporal is not ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF:
            return self._fallback(article, cutoff, policy,
                                  ContentFetchReason.POST_CUTOFF if temporal is ContentTemporalStatus.POST_CUTOFF else ContentFetchReason.ACCESS_FAILURE,
                                  response=response,
                                  access_status=(ContentAccessStatus.POST_CUTOFF if temporal is ContentTemporalStatus.POST_CUTOFF else ContentAccessStatus.TEMPORAL_UNVERIFIED),
                                  temporal_status=temporal,
                                  temporal_basis=temporal_basis,
                                  quality_warning="Page content could be mutable after the historical cutoff; no content was retained.")
        parsed = _parse_main_content(
            decoded, article.discovery_fingerprint or article.canonical_discovery_fingerprint()
        )
        if not parsed:
            return self._fallback(article, cutoff, policy, ContentFetchReason.NO_MAIN_CONTENT, response=response,
                                  access_status=ContentAccessStatus.FETCHED)
        if len(parsed) > policy.max_paragraphs or any(
            len(item.text or "") > policy.max_paragraph_characters for item in parsed
        ) or len("\n\n".join(item.text or "" for item in parsed)) > policy.max_body_characters:
            return self._fallback(article, cutoff, policy, ContentFetchReason.BODY_EXCEEDS_BOUND, response=response,
                                  access_status=ContentAccessStatus.RESPONSE_REJECTED)
        return self._available(article, cutoff, policy, response, parsed, excerpt_selector, temporal_basis)

    def _request(
        self,
        url: str,
        *,
        resolved_addresses: tuple[str, ...],
        max_response_bytes: int,
    ) -> ContentHttpResponse:
        """Make one IP-pinned request and retain at most ``limit + 1`` bytes.

        The hostname is resolved exactly once by ``fetch``. The validated
        addresses are then dialed directly, while TLS certificate validation
        and the HTTP Host header retain the original hostname. This closes the
        validate-then-resolve DNS-rebinding window and avoids proxy/environment
        routing. Reading one byte beyond the policy limit is sufficient for the
        caller to classify the response as too large without buffering it.
        """

        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        last_error: Exception | None = None
        for address in resolved_addresses:
            transport: socket.socket | None = None
            response: http.client.HTTPResponse | None = None
            try:
                transport = socket.create_connection((address, port), timeout=20.0)
                if parsed.scheme == "https":
                    transport = ssl.create_default_context().wrap_socket(
                        transport, server_hostname=host
                    )
                default_port = 443 if parsed.scheme == "https" else 80
                host_literal = f"[{host}]" if ":" in host else host
                host_header = (
                    host_literal if port == default_port else f"{host_literal}:{port}"
                )
                request_bytes = (
                    f"GET {target} HTTP/1.1\r\n"
                    f"Host: {host_header}\r\n"
                    "Accept: text/html,application/xhtml+xml\r\n"
                    "Accept-Encoding: identity\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                transport.sendall(request_bytes)
                response = http.client.HTTPResponse(transport)
                response.begin()
                body = response.read(max_response_bytes + 1)
                return ContentHttpResponse(
                    status_code=response.status,
                    headers={key.casefold(): value for key, value in response.headers.items()},
                    body=body,
                    retrieved_at=self._clock().astimezone(UTC),
                    final_url=url,
                )
            except (OSError, http.client.HTTPException, ValueError) as error:
                last_error = error
            finally:
                if response is not None:
                    response.close()
                if transport is not None:
                    transport.close()
        raise OSError("no validated content destination was reachable") from last_error

    def _available(
        self,
        article: NewsArticleMetadata,
        cutoff: datetime,
        policy: ContentFetchPolicy,
        response: ContentHttpResponse,
        parsed: tuple[ContentParagraph, ...],
        excerpt_selector: ContentExcerptSelector | None,
        temporal_basis: ContentTemporalBasis,
    ) -> ContentEvidence:
        full_body = None
        excerpts: tuple[ContentExcerpt, ...] = ()
        paragraphs = parsed
        if policy.retention_policy is ContentRetentionPolicy.FULL_BODY:
            full_body = "\n\n".join(item.text or "" for item in parsed)
        elif policy.retention_policy is ContentRetentionPolicy.BOUNDED_EXCERPTS:
            # Selection is intentionally delegated.  MVP-306 can inspect every
            # transient parsed paragraph (including middle paragraphs) before
            # this method discards unselected text.  A missing selector retains
            # no excerpts rather than silently biasing evidence toward the lead.
            selected_ids = () if excerpt_selector is None else excerpt_selector(parsed, policy)
            if len(selected_ids) > policy.max_excerpt_count or len(set(selected_ids)) != len(selected_ids):
                raise ValueError("excerpt selector exceeded the configured excerpt count or returned duplicate IDs")
            known = {item.paragraph_id: item for item in parsed}
            if any(item not in known for item in selected_ids):
                raise ValueError("excerpt selector returned an unknown paragraph ID")
            excerpt_source = tuple(known[item] for item in selected_ids)
            excerpts = tuple(
                ContentExcerpt(
                    paragraph_id=item.paragraph_id,
                    text=bound_excerpt(item.text or "", policy.max_excerpt_characters),
                    text_sha256=_text_sha256(bound_excerpt(item.text or "", policy.max_excerpt_characters)),
                )
                for item in excerpt_source
            )
            paragraphs = tuple(item.model_copy(update={"text": None}) for item in parsed)
        else:
            paragraphs = tuple(item.model_copy(update={"text": None}) for item in parsed)
        if policy.retention_policy is ContentRetentionPolicy.METADATA_ONLY or (
            policy.retention_policy is ContentRetentionPolicy.BOUNDED_EXCERPTS and not excerpts
        ):
            return self._fallback(
                article, cutoff, policy, ContentFetchReason.NO_PERMITTED_EXCERPTS,
                response=response, access_status=ContentAccessStatus.FETCHED,
                temporal_status=ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF,
                temporal_basis=temporal_basis,
                paragraphs=paragraphs,
                quality_warning="Article was parsed, but the rights policy retained no readable passage.",
            )
        return self._evidence(
            article,
            cutoff,
            policy,
            status=ContentFetchStatus.CONTENT_AVAILABLE,
            reason=ContentFetchReason.CONTENT_PARSED,
            response=response,
            access_status=ContentAccessStatus.FETCHED,
            temporal_status=ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF,
            temporal_basis=temporal_basis,
            paragraphs=paragraphs,
            excerpts=excerpts,
            full_body=full_body,
        )

    def _fallback(
        self,
        article: NewsArticleMetadata,
        cutoff: datetime,
        policy: ContentFetchPolicy,
        reason: ContentFetchReason,
        *,
        response: ContentHttpResponse | None = None,
        access_status: ContentAccessStatus = ContentAccessStatus.NOT_ATTEMPTED,
        temporal_status: ContentTemporalStatus = ContentTemporalStatus.MUTABLE_PAGE_TEMPORAL_UNVERIFIED,
        temporal_basis: ContentTemporalBasis = ContentTemporalBasis.NONE,
        paragraphs: tuple[ContentParagraph, ...] = (),
        quality_warning: str | None = None,
    ) -> ContentEvidence:
        has_permitted_summary = (
            article.provider_summary is not None
            and article.summary_status is NewsSummaryStatus.RETAINED_PERMITTED
        )
        effective_reason = (
            ContentFetchReason.PROVIDER_SUMMARY_ONLY
            if has_permitted_summary
            and reason is ContentFetchReason.ACCESS_NOT_PERMITTED
            else reason
        )
        return self._evidence(
            article,
            cutoff,
            policy,
            status=(ContentFetchStatus.SUMMARY_ONLY if has_permitted_summary else ContentFetchStatus.HEADLINE_ONLY),
            reason=effective_reason,
            response=response,
            access_status=access_status,
            temporal_status=temporal_status,
            temporal_basis=temporal_basis,
            paragraphs=paragraphs,
            quality_warning=quality_warning or _warning_for(effective_reason),
        )

    @staticmethod
    def _evidence(
        article: NewsArticleMetadata,
        cutoff: datetime,
        policy: ContentFetchPolicy,
        *,
        status: ContentFetchStatus,
        reason: ContentFetchReason,
        response: ContentHttpResponse | None = None,
        access_status: ContentAccessStatus,
        temporal_status: ContentTemporalStatus,
        temporal_basis: ContentTemporalBasis,
        paragraphs: tuple[ContentParagraph, ...] = (),
        excerpts: tuple[ContentExcerpt, ...] = (),
        full_body: str | None = None,
        quality_warning: str | None = None,
    ) -> ContentEvidence:
        fetched_at = None
        response_sha256 = None
        if (
            response is None
            and policy.source_kind is ContentSourceKind.PROVIDER
            and article.retrieved_at is not None
        ):
            fetched_at = article.retrieved_at.astimezone(UTC)
        if response is not None:
            if response.retrieved_at.tzinfo is not None and response.retrieved_at.utcoffset() is not None:
                fetched_at = response.retrieved_at.astimezone(UTC)
            response_sha256 = hashlib.sha256(response.body).hexdigest()
        response_content_type = None
        response_byte_count = None
        content_available_at = None
        final_url = None
        if response is not None:
            response_content_type = next(
                (value for key, value in response.headers.items() if key.casefold() == "content-type"), None
            )
            response_byte_count = len(response.body)
            final_url = response.final_url or article.url
            if response.content_available_at is not None and response.content_available_at.tzinfo is not None and response.content_available_at.utcoffset() is not None:
                content_available_at = response.content_available_at.astimezone(UTC)
        return ContentEvidence(
            provider_article_id=article.provider_article_id,
            source_url=article.url,
            final_url=final_url,
            source_host=_source_host(article.url),
            published_at=article.published_at,
            cutoff_at=cutoff,
            fetched_at=fetched_at,
            content_available_at=content_available_at,
            temporal_basis=temporal_basis,
            article_metadata_sha256=article.item_sha256 or article.canonical_sha256(),
            discovery_sha256=article.discovery_fingerprint or article.canonical_discovery_fingerprint(),
            policy_receipt=policy,
            policy_sha256=policy.canonical_sha256(),
            grant_id=policy.grant_id,
            grant_version=policy.grant_version,
            source_kind=policy.source_kind,
            source_ref=policy.source_ref,
            status=status,
            reason=reason,
            access_status=access_status,
            rights_status=_rights_status(policy.retention_policy),
            temporal_status=temporal_status,
            retention_policy=policy.retention_policy,
            response_sha256=response_sha256,
            response_content_type=response_content_type,
            response_byte_count=response_byte_count,
            excerpt_count_limit=policy.max_excerpt_count,
            excerpt_character_limit=policy.max_excerpt_characters,
            paragraphs=paragraphs,
            excerpts=excerpts,
            full_body=full_body,
            quality_warning=quality_warning,
        )


_BOILERPLATE_HINTS = frozenset({
    "advert", "banner", "breadcrumb", "cookie", "comment", "footer", "header",
    "menu", "nav", "newsletter", "promo", "related", "share", "sidebar", "social",
    "subscribe", "toolbar",
})
_MAIN_HINTS = frozenset({"article", "body", "content", "entry", "post", "story"})
_TEXT_TAGS = frozenset({"p", "li", "blockquote"})


@dataclass
class _Block:
    text: list[str]
    main_context: bool
    excluded: bool


class _MainContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, frozenset[str]]] = []
        self._blocks: list[_Block] = []
        self._current: _Block | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        classes = frozenset(
            token.casefold()
            for key, value in attrs
            if key.casefold() in {"class", "id", "role"} and value
            for token in re.split(r"[^a-zA-Z0-9_-]+", value)
            if token
        )
        self._stack.append((tag, classes))
        if tag in _TEXT_TAGS:
            all_tags = {item[0] for item in self._stack}
            all_hints = {hint for _, values in self._stack for hint in values}
            self._current = _Block(
                text=[],
                main_context=bool(all_tags & {"article", "main"} or all_hints & _MAIN_HINTS),
                excluded=bool(all_tags & {"nav", "footer", "header", "aside", "script", "style", "noscript"}
                              or all_hints & _BOILERPLATE_HINTS),
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _TEXT_TAGS and self._current is not None:
            self._blocks.append(self._current)
            self._current = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.text.append(data)

    def paragraphs(self) -> tuple[str, ...]:
        cleaned = [
            _normalize_text(" ".join(block.text))
            for block in self._blocks
            if not block.excluded and _normalize_text(" ".join(block.text))
        ]
        main = [
            _normalize_text(" ".join(block.text))
            for block in self._blocks
            if block.main_context and not block.excluded and _normalize_text(" ".join(block.text))
        ]
        selected = main or cleaned
        return tuple(text for text in selected if len(text) >= 20)


def _parse_main_content(html: str, discovery_sha256: str) -> tuple[ContentParagraph, ...]:
    parser = _MainContentParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError):
        return ()
    paragraphs: list[ContentParagraph] = []
    occurrences: dict[str, int] = {}
    for ordinal, text in enumerate(parser.paragraphs(), start=1):
        digest = _text_sha256(text)
        occurrence = occurrences.get(digest, 0) + 1
        occurrences[digest] = occurrence
        paragraphs.append(
            ContentParagraph(
                paragraph_id=f"content:{discovery_sha256[:16]}:p:{ordinal}:{digest[:12]}" + (f":{occurrence}" if occurrence > 1 else ""),
                ordinal=ordinal,
                text=text,
                text_sha256=digest,
            )
        )
    return tuple(paragraphs)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def bound_excerpt(value: str, maximum: int) -> str:
    """Normalize and deterministically bound text without exceeding ``maximum``."""

    if maximum < 1:
        raise ValueError("maximum excerpt characters must be positive")
    collapsed = _normalize_text(value)
    if len(collapsed) <= maximum:
        return collapsed
    return f"{collapsed[: maximum - 1].rstrip()}…"


def _text_sha256(value: str) -> str:
    return hashlib.sha256(_normalize_text(value).encode("utf-8")).hexdigest()


def _sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _charset_from_content_type(content_type: str) -> str:
    match = re.search(r"charset=([A-Za-z0-9_-]+)", content_type)
    return match.group(1) if match else "utf-8"


def _looks_like_paywall(body: bytes) -> bool:
    sample = body[:16_384].decode("utf-8", errors="ignore").casefold()
    return any(marker in sample for marker in (
        "paywall", "subscribe to continue", "subscription required", "sign in to continue",
    ))


def _warning_for(reason: ContentFetchReason) -> str:
    return {
        ContentFetchReason.PROVIDER_SUMMARY_ONLY: "Only a permitted provider summary was retained; no article body was accessed.",
        ContentFetchReason.ACCESS_NOT_PERMITTED: "Content access was not permitted; analysis is degraded to metadata.",
        ContentFetchReason.ROBOTS_DISALLOWED: "Robots policy disallowed content retrieval; analysis is degraded to metadata.",
        ContentFetchReason.PAYWALL_OR_LOGIN: "Publisher access control was encountered; EventLens did not bypass it.",
        ContentFetchReason.ACCESS_DENIED: "Publisher denied access; EventLens did not retry around the control.",
        ContentFetchReason.POST_CUTOFF: "Article was published at or after the strict research cutoff.",
        ContentFetchReason.RESPONSE_TOO_LARGE: "Response exceeded the configured content size bound.",
        ContentFetchReason.BODY_EXCEEDS_BOUND: "Parsed article exceeded the configured retention bound.",
        ContentFetchReason.UNSUPPORTED_CONTENT_TYPE: "Source did not return accessible HTML content.",
        ContentFetchReason.NO_MAIN_CONTENT: "No accessible main article content could be parsed.",
        ContentFetchReason.ACCESS_FAILURE: "Content retrieval failed without bypassing access controls.",
        ContentFetchReason.REDIRECT_REFUSED: "Redirects were refused to keep the publisher URL and access policy exact.",
    }.get(reason, "Content is unavailable; analysis is degraded to metadata.")


def _rights_status(policy: ContentRetentionPolicy) -> ContentRightsStatus:
    return {
        ContentRetentionPolicy.FULL_BODY: ContentRightsStatus.FULL_BODY_PERMITTED,
        ContentRetentionPolicy.BOUNDED_EXCERPTS: ContentRightsStatus.EXCERPTS_PERMITTED,
        ContentRetentionPolicy.METADATA_ONLY: ContentRightsStatus.METADATA_ONLY,
    }[policy]


def _temporal_status(response: ContentHttpResponse, cutoff: datetime) -> tuple[ContentTemporalStatus, ContentTemporalBasis]:
    if response.content_available_at is not None:
        value = response.content_available_at
        if value.tzinfo is None or value.utcoffset() is None:
            return ContentTemporalStatus.MUTABLE_PAGE_TEMPORAL_UNVERIFIED, ContentTemporalBasis.NONE
        if value.astimezone(UTC) < cutoff:
            return ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF, ContentTemporalBasis.SOURCE_CONTENT_TIMESTAMP
        return ContentTemporalStatus.POST_CUTOFF, ContentTemporalBasis.SOURCE_CONTENT_TIMESTAMP
    if response.retrieved_at.tzinfo is not None and response.retrieved_at.utcoffset() is not None and response.retrieved_at.astimezone(UTC) < cutoff:
        return ContentTemporalStatus.VERIFIED_BEFORE_CUTOFF, ContentTemporalBasis.RETRIEVED_BEFORE_CUTOFF
    return ContentTemporalStatus.MUTABLE_PAGE_TEMPORAL_UNVERIFIED, ContentTemporalBasis.NONE


def _source_host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").casefold().rstrip(".")


def _is_public_hostname(host: str) -> bool:
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host))
    return _is_public_ip(str(address))


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def _resolve_hostname(host: str) -> tuple[str, ...]:
    """Resolve every address so public-looking DNS names cannot target private networks."""

    return tuple(
        sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
        )
    )


def _url_allowed(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        return False
    host = _source_host(url)
    return _is_public_hostname(host) and host in allowed_hosts


__all__ = [
    "CONTENT_EVIDENCE_SCHEMA_VERSION",
    "CONTENT_PARSER_VERSION",
    "ContentAccessStatus",
    "ContentEvidence",
    "ContentExcerpt",
    "ContentExcerptSelector",
    "ContentFetchPolicy",
    "ContentFetchReason",
    "ContentFetchStatus",
    "ContentHttpResponse",
    "ContentParagraph",
    "ContentRetentionPolicy",
    "ContentRightsStatus",
    "ContentSourceKind",
    "ContentTemporalBasis",
    "ContentTemporalStatus",
    "RightsAwareContentFetcher",
    "bound_excerpt",
]
