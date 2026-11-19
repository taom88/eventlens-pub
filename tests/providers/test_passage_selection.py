from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from eventlens.ontologies.loader import OntologyCatalog
from eventlens.providers.content import (
    ContentFetchPolicy,
    ContentHttpResponse,
    ContentParagraph,
    ContentRetentionPolicy,
    ContentSourceKind,
    RightsAwareContentFetcher,
)
from eventlens.providers.models import NewsArticleMetadata
from eventlens.providers.passage_selection import (
    ArticleCandidate,
    DuplicateKind,
    PassageCandidate,
    PassageSelectionPolicy,
    PassageSelectionReason,
    PassageSelection,
    cluster_duplicates,
    passage_excerpt_selector,
    select_passages,
    sha256_hex,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _article(article_id: str, text: str, *, headline: str = "Example Systems update") -> ArticleCandidate:
    return ArticleCandidate(
        article_id=article_id,
        headline=headline,
        text=text,
        discovery_fingerprint=_hash(f"discovery:{article_id}"),
        published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


def test_selects_the_material_middle_paragraph_not_the_misleading_lead() -> None:
    result = select_passages(
        article_id="story-1",
        issuer_terms=("Example Systems", "DEMO"),
        paragraphs=(
            PassageCandidate(
                paragraph_id="p1",
                ordinal=1,
                text="Example Systems shares moved in early trading after a broad market selloff.",
            ),
            PassageCandidate(
                paragraph_id="p2",
                ordinal=2,
                text=(
                    "Example Systems raised full-year revenue guidance to $10 billion, and its CFO said "
                    "Cloud demand remains ahead of expectations."
                ),
            ),
            PassageCandidate(paragraph_id="p3", ordinal=3, text="The company will report results next week."),
        ),
        policy=PassageSelectionPolicy(max_passages_per_article=1),
    )

    selected = result.selected_passages
    assert [item.paragraph_id for item in selected] == ["p2"]
    assert PassageSelectionReason.GUIDANCE in selected[0].selection_reasons
    assert PassageSelectionReason.QUANTIFIED_FACT in selected[0].selection_reasons
    assert PassageSelectionReason.MANAGEMENT_QUOTE in selected[0].selection_reasons
    assert result.selection_sha256


def test_ontology_aliases_raise_the_relevant_paragraph_and_are_exposed() -> None:
    ontology = OntologyCatalog.packaged().resolve("sector.internet_platform")
    result = select_passages(
        article_id="story-ontology",
        issuer_terms=("Example Systems",),
        ontology=ontology,
        paragraphs=(
            PassageCandidate(paragraph_id="lead", ordinal=1, text="Example Systems shares were mixed in light trading."),
            PassageCandidate(
                paragraph_id="middle",
                ordinal=2,
                text="Example Systems cloud revenue growth accelerated as AI capacity expanded.",
            ),
        ),
        policy=PassageSelectionPolicy(max_passages_per_article=1),
    )

    selected = result.selected_passages[0]
    assert selected.paragraph_id == "middle"
    assert PassageSelectionReason.ONTOLOGY_CRITICAL in selected.selection_reasons
    assert "platform.cloud_growth" in selected.ontology_slot_ids


def test_duplicate_wire_copies_cluster_exactly_with_stable_id() -> None:
    stories = (
        _article("wire-b", "Example Systems raised revenue guidance to $10 billion after earnings."),
        _article("wire-a", "Example Systems raised revenue guidance to $10 billion after earnings."),
    )

    first = cluster_duplicates(stories)
    second = cluster_duplicates(tuple(reversed(stories)))

    assert first == second
    assert first.clusters[0].kind is DuplicateKind.EXACT
    assert first.clusters[0].article_ids == ("wire-a", "wire-b")
    assert first.clusters[0].cluster_id == "content-cluster:" + _hash(
        "|".join(sorted((_hash("discovery:wire-a"), _hash("discovery:wire-b"))))
    )[:16]


def test_near_duplicate_and_conflicting_guidance_do_not_share_cluster() -> None:
    result = cluster_duplicates(
        (
            _article("copy-a", "Example Systems raised 2026 revenue guidance to $10 billion after strong cloud demand."),
            _article("copy-b", "Example Systems raised 2026 revenue guidance to $10 billion after strong cloud demand on Monday."),
            _article("conflict", "Example Systems cut 2026 revenue guidance to $8 billion after weak cloud demand."),
        ),
        policy=PassageSelectionPolicy(near_duplicate_threshold=0.50),
    )

    assert len(result.clusters) == 1
    assert result.clusters[0].kind is DuplicateKind.NEAR
    assert result.clusters[0].article_ids == ("copy-a", "copy-b")
    assert result.unclustered_article_ids == ("conflict",)


def test_token_boundary_reordered_text_time_window_and_revenue_conflicts_are_safe() -> None:
    selection = select_passages(
        article_id="boundary",
        issuer_terms=("demo",),
        paragraphs=(PassageCandidate(paragraph_id="p", ordinal=1, text="Metadata risk rose."),),
    )
    assert not selection.selected_passages

    first = _article("first", "Example Systems raised revenue guidance to $10 billion after strong cloud demand.")
    reordered = _article("reordered", "Strong cloud demand led Example Systems to raise revenue guidance to $10 billion.")
    old = _article("old", first.text)
    old = old.model_copy(update={"published_at": old.published_at + timedelta(days=8)})
    conflict = _article("revenue-conflict", "Example Systems reported revenue of $8 billion after strong cloud demand.")
    result = cluster_duplicates((first, reordered, old, conflict), policy=PassageSelectionPolicy(near_duplicate_threshold=0.50))

    assert not result.clusters
    assert result.unclustered_article_ids == ("first", "old", "reordered", "revenue-conflict")


def test_issuer_context_allows_pronoun_metric_and_checksum_binds_full_policy() -> None:
    result = select_passages(
        article_id="context",
        issuer_terms=("Example Systems",),
        issuer_context=True,
        paragraphs=(
            PassageCandidate(
                paragraph_id="pronoun",
                ordinal=2,
                text="It raised revenue guidance to $10 billion after the call.",
            ),
        ),
    )
    assert result.selected_passages[0].paragraph_id == "pronoun"
    assert PassageSelectionReason.GUIDANCE in result.selected_passages[0].selection_reasons

    forged = result.model_dump(mode="json")
    forged["policy"]["max_total_excerpt_characters"] = 1_000
    with pytest.raises(ValueError, match="selection_sha256"):
        PassageSelection.model_validate(forged)


def test_selection_output_is_bounded_and_ties_are_stable() -> None:
    result = select_passages(
        article_id="bounded",
        issuer_terms=("Example Systems",),
        paragraphs=(
            PassageCandidate(paragraph_id="z", ordinal=3, text="Example Systems faces supply-chain risk."),
            PassageCandidate(paragraph_id="a", ordinal=1, text="Example Systems faces supply-chain risk."),
            PassageCandidate(paragraph_id="m", ordinal=2, text="Example Systems faces supply-chain risk."),
        ),
        policy=PassageSelectionPolicy(
            max_passages_per_article=3,
            max_excerpt_characters=100,
            max_total_excerpt_characters=300,
        ),
    )

    assert [item.paragraph_id for item in result.selected_passages] == ["a", "m", "z"]
    assert sum(len(item.excerpt) for item in result.selected_passages) <= 300
    assert all(len(item.excerpt) <= 100 for item in result.selected_passages)


def test_content_selection_enforces_the_fetch_grant_before_exporting_text() -> None:
    policy = ContentFetchPolicy(
        source_kind=ContentSourceKind.ISSUER,
        grant_id="issuer-release-v1",
        grant_version="1",
        source_ref="https://ir.example.com/terms",
        allowed_hosts=("ir.example.com",),
        access_permitted=True,
        robots_permitted=True,
        retention_policy=ContentRetentionPolicy.FULL_BODY,
        max_excerpt_count=1,
        max_excerpt_characters=120,
    )
    transient = (
        ContentParagraph(paragraph_id="lead", text_sha256=_hash("Example Systems shares moved."), ordinal=1, text="Example Systems shares moved."),
        ContentParagraph(paragraph_id="middle", text_sha256=_hash("Example Systems raised revenue guidance to $10 billion."), ordinal=2, text="Example Systems raised revenue guidance to $10 billion."),
    )
    selector = passage_excerpt_selector(
        issuer_terms=("Example Systems",),
        policy=PassageSelectionPolicy(max_passages_per_article=1, max_excerpt_characters=120, max_total_excerpt_characters=120),
    )
    assert selector(transient, policy) == ("middle",)

    with pytest.raises(ValueError, match="exceeds the content retention grant"):
        passage_excerpt_selector(
            issuer_terms=("Example Systems",),
            policy=PassageSelectionPolicy(
                max_passages_per_article=2,
                max_excerpt_characters=120,
                max_total_excerpt_characters=240,
            ),
        )(transient, policy)


def test_mvp305_roundtrip_preserves_exact_long_excerpt_and_bound_receipt() -> None:
    now = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
    cutoff = datetime(2026, 7, 2, 16, tzinfo=timezone.utc)
    article = NewsArticleMetadata(
        ticker="DEMO",
        provider_article_id="wire-1",
        headline="Example Systems guidance",
        url="https://news.example/wire-1",
        published_at=datetime(2026, 7, 2, 10, tzinfo=timezone.utc),
    )
    fetch_policy = ContentFetchPolicy(
        source_kind=ContentSourceKind.THIRD_PARTY,
        grant_id="news-excerpts-v1",
        grant_version="v1",
        source_ref="terms:news.example",
        allowed_hosts=("news.example",),
        access_permitted=True,
        robots_permitted=True,
        retention_policy=ContentRetentionPolicy.BOUNDED_EXCERPTS,
        max_excerpt_count=1,
        max_excerpt_characters=800,
    )
    long_middle = "Example Systems raised revenue guidance to $10 billion. " + "x" * 900
    selector = passage_excerpt_selector(
        issuer_terms=("Example Systems",),
        policy=PassageSelectionPolicy(
            max_passages_per_article=1,
            max_excerpt_characters=800,
            max_total_excerpt_characters=800,
        ),
    )
    evidence = RightsAwareContentFetcher(
        clock=lambda: now, resolver=lambda _: ("93.184.216.34",)
    ).fetch(
        article,
        cutoff_at=cutoff,
        policy=fetch_policy,
        excerpt_selector=selector,
        request=lambda _: ContentHttpResponse(
            status_code=200,
            headers={"content-type": "text/html"},
            body=(
                "<article><p>Example Systems shares traded lower on macro concern.</p>"
                f"<p>{long_middle}</p></article>"
            ).encode(),
            retrieved_at=now,
        ),
    )
    selection = selector.selection_for(article, evidence)

    assert selection.policy.max_total_excerpt_characters == 800
    assert selection.content_policy_sha256 == fetch_policy.canonical_sha256()
    assert selection.grant_id == "news-excerpts-v1"
    assert selection.evidence_sha256 == evidence.evidence_sha256
    assert selection.selected_passages[0].excerpt == evidence.excerpts[0].text
    assert selection.selected_passages[0].excerpt_sha256 == evidence.excerpts[0].text_sha256
    assert len(selection.selected_passages[0].excerpt) == 800

    with pytest.raises(ValueError, match="grant does not match"):
        selector.selection_for(article, evidence.model_copy(update={"grant_id": "forged"}))
    with pytest.raises(ValueError, match="checksum does not match"):
        selector.selection_for(article, evidence.model_copy(update={"evidence_sha256": "0" * 64}))


def test_rechecksummed_forged_selection_and_cluster_artifacts_fail_semantics() -> None:
    selection = select_passages(
        article_id="receipt",
        issuer_terms=("Example Systems",),
        paragraphs=(PassageCandidate(paragraph_id="p", ordinal=1, text="Example Systems raised revenue guidance to $10 billion."),),
    )
    forged_selection = selection.model_dump(mode="json")
    forged_selection["policy"]["max_total_excerpt_characters"] = 10_000
    forged_selection["selected_passages"][0]["excerpt"] = "x" * 5_000
    forged_selection["selected_passages"][0]["excerpt_sha256"] = _hash("x" * 5_000)
    forged_selection["selection_sha256"] = sha256_hex({key: value for key, value in forged_selection.items() if key != "selection_sha256"})
    with pytest.raises(ValueError, match="per-passage"):
        PassageSelection.model_validate(forged_selection)

    forged_version = selection.model_dump(mode="json")
    forged_version["policy_version"] = "forged-version"
    forged_version["selection_sha256"] = sha256_hex({key: value for key, value in forged_version.items() if key != "selection_sha256"})
    with pytest.raises(ValueError, match="policy_version"):
        PassageSelection.model_validate(forged_version)

    forged_score = selection.model_dump(mode="json")
    forged_score["selected_passages"][0]["score"] = 999
    forged_score["selection_sha256"] = sha256_hex({key: value for key, value in forged_score.items() if key != "selection_sha256"})
    with pytest.raises(ValueError, match="score does not match"):
        PassageSelection.model_validate(forged_score)

    forged_reasons = selection.model_dump(mode="json")
    forged_reasons["selected_passages"][0]["selection_reasons"] = []
    forged_reasons["selected_passages"][0]["score"] = 0
    forged_reasons["selection_sha256"] = sha256_hex({key: value for key, value in forged_reasons.items() if key != "selection_sha256"})
    with pytest.raises(ValueError, match="at least one"):
        PassageSelection.model_validate(forged_reasons)

    near = cluster_duplicates(
        (
            _article("near-a", "Example Systems raised 2026 revenue guidance to $10 billion after strong cloud demand."),
            _article("near-b", "Example Systems raised 2026 revenue guidance to $10 billion after strong cloud demand on Monday."),
        ),
        policy=PassageSelectionPolicy(near_duplicate_threshold=0.50),
    )
    forged_cluster = near.model_dump(mode="json")
    forged_cluster["clusters"][0]["cluster_id"] = "content-cluster:" + "0" * 16
    forged_cluster["clustering_sha256"] = sha256_hex({key: value for key, value in forged_cluster.items() if key != "clustering_sha256"})
    with pytest.raises(ValueError, match="cluster ID"):
        type(near).model_validate(forged_cluster)

    forged_threshold = near.model_dump(mode="json")
    forged_threshold["policy"]["near_duplicate_threshold"] = 0.99
    forged_threshold["clustering_sha256"] = sha256_hex({key: value for key, value in forged_threshold.items() if key != "clustering_sha256"})
    with pytest.raises(ValueError, match="non-duplicate"):
        type(near).model_validate(forged_threshold)

    forged_empty_partition = near.model_dump(mode="json")
    forged_empty_partition["clusters"] = []
    forged_empty_partition["unclustered_article_ids"] = ["near-a", "near-b"]
    forged_empty_partition["clustering_sha256"] = sha256_hex({key: value for key, value in forged_empty_partition.items() if key != "clustering_sha256"})
    with pytest.raises(ValueError, match="deterministic input partition"):
        type(near).model_validate(forged_empty_partition)

    forged_representative = near.model_dump(mode="json")
    forged_representative["clusters"][0]["representative_article_id"] = "near-b"
    forged_representative["clustering_sha256"] = sha256_hex({key: value for key, value in forged_representative.items() if key != "clustering_sha256"})
    with pytest.raises(ValueError, match="representative"):
        type(near).model_validate(forged_representative)

    unrelated = cluster_duplicates((_article("lone-a", "Example Systems update A."), _article("lone-b", "Example Systems update B.")))
    forged_unclustered = unrelated.model_dump(mode="json")
    forged_unclustered["unclustered_article_ids"] = ["lone-a", "lone-a"]
    forged_unclustered["clustering_sha256"] = sha256_hex({key: value for key, value in forged_unclustered.items() if key != "clustering_sha256"})
    with pytest.raises(ValueError, match="unclustered article IDs must be unique"):
        type(unrelated).model_validate(forged_unclustered)
