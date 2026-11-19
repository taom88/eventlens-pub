"""MVP-305 rights-aware content evidence tests."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace

import pytest

from eventlens.providers import (
    ContentFetchPolicy,
    ContentFetchReason,
    ContentFetchStatus,
    ContentHttpResponse,
    ContentRetentionPolicy,
    ContentSourceKind,
    NewsArticleMetadata,
    NewsSummaryStatus,
    RightsAwareContentFetcher,
    bound_excerpt,
)
from eventlens.providers.passage_selection import select_content_passages


NOW = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 7, 2, 16, tzinfo=timezone.utc)


def _article(**changes: object) -> NewsArticleMetadata:
    values: dict[str, object] = {
        "ticker": "DEMO",
        "provider_article_id": "source-article-7",
        "headline": "Example Systems announces cloud update",
        "url": "https://issuer.example/news/cloud-update",
        "published_at": datetime(2026, 7, 2, 10, tzinfo=timezone.utc),
    }
    values.update(changes)
    return NewsArticleMetadata(**values)


def _response(
    body: bytes | str,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
) -> ContentHttpResponse:
    return ContentHttpResponse(
        status_code=status,
        headers={"Content-Type": content_type},
        body=body.encode() if isinstance(body, str) else body,
        retrieved_at=NOW,
    )


def _fetcher() -> RightsAwareContentFetcher:
    return RightsAwareContentFetcher(
        clock=lambda: NOW,
        resolver=lambda _: ("93.184.216.34",),
    )


def _policy(
    retention: ContentRetentionPolicy = ContentRetentionPolicy.FULL_BODY,
    **changes: object,
) -> ContentFetchPolicy:
    values: dict[str, object] = {
        "source_kind": ContentSourceKind.ISSUER,
        "grant_id": "test-grant",
        "grant_version": "v1",
        "source_ref": "terms:test",
        "allowed_hosts": ("issuer.example", "news.example"),
        "access_permitted": True,
        "robots_permitted": True,
        "retention_policy": retention,
    }
    values.update(changes)
    return ContentFetchPolicy(**values)


@pytest.mark.parametrize("source_kind", [ContentSourceKind.ISSUER, ContentSourceKind.SEC])
def test_issuer_and_sec_permitted_bodies_are_parsed_with_stable_receipts(source_kind) -> None:
    html = """
    <html><body>
      <nav><p>Navigation and cookie preferences should disappear.</p></nav>
      <main><article><p>Revenue grew 18 percent due to cloud demand.</p>
      <p>Management raised its operating-margin outlook.</p></article></main>
      <footer><p>Subscribe for unrelated updates.</p></footer>
    </body></html>
    """
    policy = _policy(source_kind=source_kind)
    first = _fetcher().fetch(_article(), cutoff_at=CUTOFF, policy=policy, request=lambda _: _response(html))
    second = _fetcher().fetch(_article(), cutoff_at=CUTOFF, policy=policy, request=lambda _: _response(html))

    assert first.status is ContentFetchStatus.CONTENT_AVAILABLE
    assert first.reason is ContentFetchReason.CONTENT_PARSED
    assert first.retention_policy is ContentRetentionPolicy.FULL_BODY
    assert first.full_body == "Revenue grew 18 percent due to cloud demand.\n\nManagement raised its operating-margin outlook."
    assert "Navigation" not in first.full_body
    assert "Subscribe" not in first.full_body
    assert [(item.paragraph_id, item.text_sha256) for item in first.paragraphs] == [
        (item.paragraph_id, item.text_sha256) for item in second.paragraphs
    ]


def test_accessible_third_party_article_keeps_only_bounded_permitted_excerpts() -> None:
    html = """
    <article>
      <p>First material fact for the company and its cloud segment.</p>
      <p>Second material fact contains guidance and a quantitative change.</p>
      <p>Third material fact should not become a retained excerpt.</p>
    </article>
    """
    evidence = _fetcher().fetch(
        _article(url="https://news.example/article"),
        cutoff_at=CUTOFF,
        policy=_policy(
            ContentRetentionPolicy.BOUNDED_EXCERPTS,
            source_kind=ContentSourceKind.THIRD_PARTY,
            max_excerpt_count=2,
            max_excerpt_characters=20,
        ),
        request=lambda _: _response(html),
        excerpt_selector=lambda paragraphs, _: tuple(item.paragraph_id for item in paragraphs[:2]),
    )

    assert evidence.status is ContentFetchStatus.CONTENT_AVAILABLE
    assert evidence.full_body is None
    assert [paragraph.text for paragraph in evidence.paragraphs] == [None, None, None]
    assert [excerpt.text for excerpt in evidence.excerpts] == [
        bound_excerpt("First material fact for the company and its cloud segment.", 20),
        bound_excerpt("Second material fact contains guidance and a quantitative change.", 20),
    ]
    assert all(excerpt.paragraph_id in {item.paragraph_id for item in evidence.paragraphs} for excerpt in evidence.excerpts)


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (_response("<html><p>Subscribe to continue reading.</p></html>"), ContentFetchReason.PAYWALL_OR_LOGIN),
        (_response("denied", status=403), ContentFetchReason.ACCESS_DENIED),
        (_response("offline", status=503), ContentFetchReason.ACCESS_FAILURE),
    ],
)
def test_denied_paywalled_and_access_failure_are_visible_headline_only(response, expected_reason) -> None:
    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(), request=lambda _: response
    )

    assert evidence.status is ContentFetchStatus.HEADLINE_ONLY
    assert evidence.reason is expected_reason
    assert evidence.full_body is None
    assert not evidence.paragraphs
    assert evidence.quality_warning


def test_permitted_provider_summary_is_summary_only_when_content_access_fails() -> None:
    article = _article(
        provider_summary="A permitted provider summary.",
        summary_status=NewsSummaryStatus.RETAINED_PERMITTED,
    )
    evidence = _fetcher().fetch(
        article, cutoff_at=CUTOFF, policy=_policy(), request=lambda _: _response("paywall", status=402)
    )

    assert evidence.status is ContentFetchStatus.SUMMARY_ONLY
    assert evidence.reason is ContentFetchReason.PAYWALL_OR_LOGIN
    assert evidence.full_body is None


def test_access_controls_are_fail_closed_without_a_request() -> None:
    calls = 0

    def request(_: str) -> ContentHttpResponse:
        nonlocal calls
        calls += 1
        return _response("<article><p>Should never be fetched.</p></article>")

    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF,
        policy=_policy(access_permitted=False), request=request,
    )
    assert calls == 0
    assert evidence.status is ContentFetchStatus.HEADLINE_ONLY
    assert evidence.reason is ContentFetchReason.ACCESS_NOT_PERMITTED

    robots = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF,
        policy=_policy(robots_permitted=False), request=request,
    )
    assert calls == 0
    assert robots.reason is ContentFetchReason.ROBOTS_DISALLOWED


def test_cutoff_is_strict_and_checked_before_access_or_network() -> None:
    calls = 0

    def request(_: str) -> ContentHttpResponse:
        nonlocal calls
        calls += 1
        return _response("<article><p>Future evidence.</p></article>")

    evidence = _fetcher().fetch(
        _article(published_at=CUTOFF), cutoff_at=CUTOFF, policy=_policy(), request=request
    )
    assert calls == 0
    assert evidence.status is ContentFetchStatus.HEADLINE_ONLY
    assert evidence.reason is ContentFetchReason.POST_CUTOFF


@pytest.mark.parametrize(
    ("retention", "expects_body", "expects_excerpts"),
    [
        (ContentRetentionPolicy.FULL_BODY, True, False),
        (ContentRetentionPolicy.BOUNDED_EXCERPTS, False, True),
        (ContentRetentionPolicy.METADATA_ONLY, False, False),
    ],
)
def test_retention_modes_never_store_more_than_policy_allows(retention, expects_body, expects_excerpts) -> None:
    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(retention),
        request=lambda _: _response("<article><p>Accessible source content for rights testing.</p></article>"),
        excerpt_selector=(
            (lambda paragraphs, _: (paragraphs[0].paragraph_id,))
            if retention is ContentRetentionPolicy.BOUNDED_EXCERPTS else None
        ),
    )
    expected_status = (
        ContentFetchStatus.HEADLINE_ONLY
        if retention is ContentRetentionPolicy.METADATA_ONLY else ContentFetchStatus.CONTENT_AVAILABLE
    )
    assert evidence.status is expected_status
    assert bool(evidence.full_body) is expects_body
    assert bool(evidence.excerpts) is expects_excerpts
    if retention is not ContentRetentionPolicy.FULL_BODY:
        assert all(paragraph.text is None for paragraph in evidence.paragraphs)


def test_response_and_body_size_bounds_degrade_without_partial_retention() -> None:
    response_bound = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(max_response_bytes=40),
        request=lambda _: _response("<article><p>" + "x" * 100 + "</p></article>"),
    )
    assert response_bound.status is ContentFetchStatus.HEADLINE_ONLY
    assert response_bound.reason is ContentFetchReason.RESPONSE_TOO_LARGE
    assert not response_bound.paragraphs

    body_bound = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(max_body_characters=30),
        request=lambda _: _response("<article><p>" + "a long accessible paragraph beyond the body bound" + "</p></article>"),
    )
    assert body_bound.status is ContentFetchStatus.HEADLINE_ONLY
    assert body_bound.reason is ContentFetchReason.BODY_EXCEEDS_BOUND
    assert body_bound.full_body is None


def test_historical_mutable_page_is_not_retained_without_pre_cutoff_content_proof() -> None:
    historical_cutoff = datetime(2026, 7, 1, 16, tzinfo=timezone.utc)
    evidence = _fetcher().fetch(
        _article(published_at=datetime(2026, 7, 1, 10, tzinfo=timezone.utc)),
        cutoff_at=historical_cutoff,
        policy=_policy(),
        request=lambda _: _response("<article><p>Mutable page content.</p></article>"),
    )
    assert evidence.status is ContentFetchStatus.HEADLINE_ONLY
    assert evidence.access_status.value == "temporal_unverified"
    assert evidence.temporal_status.value == "mutable_page_temporal_unverified"
    assert not evidence.paragraphs


def test_evidence_binds_cutoff_and_discovery_fingerprint() -> None:
    article = _article()
    evidence = _fetcher().fetch(
        article, cutoff_at=CUTOFF, policy=_policy(),
        request=lambda _: _response("<article><p>Cutoff-safe accessible content.</p></article>"),
    )
    assert evidence.cutoff_at == CUTOFF
    assert evidence.discovery_sha256 == article.discovery_fingerprint
    assert evidence.article_metadata_sha256 == article.item_sha256
    assert evidence.source_kind is ContentSourceKind.ISSUER
    assert evidence.source_ref == "terms:test"
    assert evidence.parser_version
    assert evidence.evidence_sha256 == evidence.content_checksum()


def test_source_timestamp_is_authoritative_over_pre_cutoff_retrieval() -> None:
    response = replace(
        _response("<article><p>Page text that became available too late.</p></article>"),
        content_available_at=CUTOFF,
    )
    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(), request=lambda _: response
    )
    assert evidence.status is ContentFetchStatus.HEADLINE_ONLY
    assert evidence.reason is ContentFetchReason.POST_CUTOFF
    assert evidence.temporal_status.value == "post_cutoff"
    assert evidence.content_available_at == CUTOFF


def test_grant_host_scope_and_private_targets_are_rejected_before_network() -> None:
    calls = 0

    def request(_: str) -> ContentHttpResponse:
        nonlocal calls
        calls += 1
        return _response("<article><p>Never fetched.</p></article>")

    out_of_scope = _fetcher().fetch(
        _article(url="https://other.example/a"), cutoff_at=CUTOFF, policy=_policy(), request=request
    )
    assert calls == 0
    assert out_of_scope.reason is ContentFetchReason.URL_NOT_ALLOWED

    with pytest.raises(ValueError, match="public hostnames"):
        _policy(allowed_hosts=("127.0.0.1",))


def test_public_hostname_resolving_to_private_address_is_rejected_before_network() -> None:
    calls = 0

    def request(_: str) -> ContentHttpResponse:
        nonlocal calls
        calls += 1
        return _response("<article><p>Never fetched.</p></article>")

    fetcher = RightsAwareContentFetcher(
        clock=lambda: NOW,
        resolver=lambda _: ("127.0.0.1",),
    )
    evidence = fetcher.fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(), request=request
    )

    assert calls == 0
    assert evidence.reason is ContentFetchReason.URL_NOT_ALLOWED


def test_default_fetch_dials_the_validated_address_not_the_hostname(
    monkeypatch,
) -> None:
    attempts = []

    def refuse_connection(address, timeout):
        attempts.append((address, timeout))
        raise OSError("deliberate test refusal")

    monkeypatch.setattr(
        "eventlens.providers.content.socket.create_connection", refuse_connection
    )
    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy()
    )

    assert attempts == [(('93.184.216.34', 443), 20.0)]
    assert evidence.reason is ContentFetchReason.ACCESS_FAILURE


def test_default_fetch_reads_only_one_byte_beyond_the_response_limit(
    monkeypatch,
) -> None:
    read_sizes = []

    class FakeSocket:
        def sendall(self, value):
            assert b"Host: issuer.example\r\n" in value

        def close(self):
            pass

    class FakeHeaders:
        def items(self):
            return (("Content-Type", "text/html"),)

    class FakeResponse:
        status = 200
        headers = FakeHeaders()

        def __init__(self, transport):
            assert isinstance(transport, FakeSocket)

        def begin(self):
            pass

        def read(self, amount):
            read_sizes.append(amount)
            return b"x" * amount

        def close(self):
            pass

    monkeypatch.setattr(
        "eventlens.providers.content.socket.create_connection",
        lambda address, timeout: FakeSocket(),
    )
    monkeypatch.setattr(
        "eventlens.providers.content.ssl.create_default_context",
        lambda: type("Context", (), {"wrap_socket": lambda self, sock, server_hostname: sock})(),
    )
    monkeypatch.setattr(
        "eventlens.providers.content.http.client.HTTPResponse", FakeResponse
    )

    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(max_response_bytes=40)
    )

    assert read_sizes == [41]
    assert evidence.reason is ContentFetchReason.RESPONSE_TOO_LARGE


def test_content_evidence_rejects_forged_status_and_retention_combinations() -> None:
    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(),
        request=lambda _: _response("<article><p>Auditable permitted paragraph content.</p><p>Second auditable paragraph.</p></article>"),
    )
    forged = evidence.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["reason"] = ContentFetchReason.ACCESS_FAILURE.value
    with pytest.raises(ValueError, match="fetched parsed-content"):
        type(evidence).model_validate(forged)

    forged = evidence.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["paragraphs"] = list(reversed(forged["paragraphs"]))
    with pytest.raises(ValueError, match="ordinals"):
        type(evidence).model_validate(forged)

    bounded = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF,
        policy=_policy(ContentRetentionPolicy.BOUNDED_EXCERPTS, max_excerpt_characters=12),
        request=lambda _: _response("<article><p>Auditable permitted paragraph content.</p></article>"),
        excerpt_selector=lambda paragraphs, _: (paragraphs[0].paragraph_id,),
    )
    forged = bounded.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["excerpts"][0]["text"] = "x" * 13
    forged["excerpts"][0]["text_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="excerpt"):
        type(bounded).model_validate(forged)


def test_direct_model_load_rejects_rechecksummed_temporal_and_schema_forgeries() -> None:
    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF, policy=_policy(),
        request=lambda _: _response("<article><p>Auditable retained content.</p></article>"),
    )
    forged = evidence.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["temporal_basis"] = "retrieved_before_cutoff"
    forged["fetched_at"] = CUTOFF.isoformat()
    with pytest.raises(ValueError, match="fetched_at strictly before cutoff"):
        type(evidence).model_validate(forged)

    forged = evidence.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["temporal_basis"] = "source_content_timestamp"
    forged["content_available_at"] = CUTOFF.isoformat()
    with pytest.raises(ValueError, match="at-or-after-cutoff source timestamp"):
        type(evidence).model_validate(forged)

    forged = evidence.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["status"] = "headline_only"
    forged["temporal_status"] = "post_cutoff"
    forged["temporal_basis"] = "none"
    forged["full_body"] = None
    for paragraph in forged["paragraphs"]:
        paragraph["text"] = None
    with pytest.raises(ValueError, match="post_cutoff temporal status"):
        type(evidence).model_validate(forged)

    forged = evidence.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["schema_version"] = "eventlens.content-evidence.v999"
    with pytest.raises(ValueError, match="Input should be"):
        type(evidence).model_validate(forged)

    forged = evidence.model_dump(mode="json")
    forged["evidence_sha256"] = None
    forged["source_ref"] = "forged:other-terms"
    with pytest.raises(ValueError, match="source authorization fields"):
        type(evidence).model_validate(forged)

    # An attacker who recomputes both checksums has produced a different,
    # internally consistent artifact, not evidence authorized by the original
    # policy.  Consumers must compare the embedded receipt to their supplied
    # policy rather than trust a digest alone.
    forged["policy_receipt"]["source_ref"] = "forged:other-terms"
    forged["policy_sha256"] = ContentFetchPolicy.model_validate(
        forged["policy_receipt"]
    ).canonical_sha256()
    rechecksummed = type(evidence).model_validate(forged)
    assert rechecksummed.evidence_sha256 != evidence.evidence_sha256
    assert rechecksummed.source_ref == "forged:other-terms"
    with pytest.raises(ValueError, match="policy receipt does not match"):
        select_content_passages(
            rechecksummed,
            article=_article(),
            fetch_policy=_policy(),
            issuer_terms=("Example Systems",),
        )


def test_shared_excerpt_bound_is_deterministic_and_retained_exactly() -> None:
    long = "A deliberately long  excerpt that must be normalized and bounded consistently."
    expected = bound_excerpt(long, 24)
    assert len(expected) <= 24
    assert expected.endswith("…")
    evidence = _fetcher().fetch(
        _article(), cutoff_at=CUTOFF,
        policy=_policy(ContentRetentionPolicy.BOUNDED_EXCERPTS, max_excerpt_characters=24),
        request=lambda _: _response(f"<article><p>{long}</p></article>"),
        excerpt_selector=lambda paragraphs, _: (paragraphs[0].paragraph_id,),
    )
    assert evidence.excerpts[0].text == expected
    assert evidence.excerpts[0].text_sha256
