from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eventlens.context_cards import build_news_receipts
from eventlens.context_cards.evidence import NewsEvidencePhase, NewsRightsStatus
from eventlens.providers.models import (
    CompanyNewsMetadata,
    NewsArticleMetadata,
    ProviderOrigin,
    ProviderProvenance,
    ProviderResponse,
)
from eventlens.providers.composite import FreeFirstNewsProvider


def _response(
    published_at: datetime = datetime(2026, 7, 1, 21, tzinfo=timezone.utc),
) -> ProviderResponse[CompanyNewsMetadata]:
    provenance = ProviderProvenance(
        provider_name="alpha_vantage", provider_version="test", origin=ProviderOrigin.FIXTURE,
        retrieved_at=datetime(2026, 7, 2, 1, tzinfo=timezone.utc),
        request_fingerprint="a" * 64, raw_response_sha256="b" * 64,
        quota_consumed=False, data_quality="complete",
    )
    article = NewsArticleMetadata(
        ticker="DEMO", provider_article_id="av-1", headline="Cloud results",
        url="https://example.com/a", published_at=published_at,
        ticker_relevance_score=Decimal("0.9"), ticker_sentiment_score=Decimal("0.2"),
        ticker_sentiment_label="Bullish",
    )
    return ProviderResponse(provenance=provenance, data=CompanyNewsMetadata(ticker="DEMO", articles=(article,)))


def test_receipt_assembler_binds_provider_hash_and_provenance() -> None:
    receipt = build_news_receipts(
        _response(), query_start=date(2026, 7, 1), query_end=date(2026, 7, 2),
        phase=NewsEvidencePhase.PRE_EVENT, cutoff_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )[0]
    assert len(receipt.item_sha) == 64
    assert receipt.response_sha == "b" * 64
    assert receipt.retrieved_at is not None
    assert receipt.rights_status is NewsRightsStatus.PROVIDER_TERMS_REVIEW_PENDING
    assert receipt.relevance_value == Decimal("0.9")


def test_receipt_assembler_rejects_missing_row_hash() -> None:
    response = _response()
    article = response.data.articles[0].model_copy()
    object.__setattr__(article, "item_sha256", None)
    bad = response.model_copy(update={"data": CompanyNewsMetadata.model_construct(ticker="DEMO", articles=(article,))})
    with pytest.raises(ValueError, match="item_sha256"):
        build_news_receipts(bad, query_start=date(2026, 7, 1), query_end=date(2026, 7, 2), phase=NewsEvidencePhase.PRE_EVENT, cutoff_at=datetime(2026, 7, 2, tzinfo=timezone.utc))


def test_news_fallback_preserves_provider_and_reason() -> None:
    class Primary:
        def get_company_news(self, *args, **kwargs):
            raise RuntimeError("not caught programmer errors")

    class Fallback:
        def get_company_news(self, *args, **kwargs):
            return _response()

    # A provider entitlement error is the supported fallback path.
    from eventlens.providers.errors import ProviderEntitlementError
    class EntitledPrimary(Primary):
        def get_company_news(self, *args, **kwargs):
            raise ProviderEntitlementError("premium")

    result = FreeFirstNewsProvider(primary=EntitledPrimary(), fallback=Fallback()).get_company_news(
        "DEMO", date(2026, 7, 1), date(2026, 7, 2), cutoff_at=datetime(2026, 7, 2, tzinfo=timezone.utc)
    )
    assert result.provenance.provider_name == "alpha_vantage"
    assert result.provenance.warnings[-1].code == "news_fallback"


def test_receipt_assembler_rejects_stale_row_hash_after_metadata_tamper() -> None:
    response = _response()
    article = response.data.articles[0].model_copy(update={"headline": "tampered"})
    bad = response.model_copy(
        update={
            "data": CompanyNewsMetadata.model_construct(
                ticker="DEMO", articles=(article,)
            )
        }
    )

    with pytest.raises(ValueError, match="canonical metadata"):
        build_news_receipts(
            bad,
            query_start=date(2026, 7, 1),
            query_end=date(2026, 7, 2),
            phase=NewsEvidencePhase.PRE_EVENT,
            cutoff_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )


def test_blocked_news_rights_cannot_be_assembled_into_a_receipt() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="blocked"):
        build_news_receipts(
            _response(),
            query_start=date(2026, 7, 1),
            query_end=date(2026, 7, 2),
            phase=NewsEvidencePhase.PRE_EVENT,
            cutoff_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            rights_status=NewsRightsStatus.BLOCKED,
        )


def test_news_receipt_rejects_non_utc_cutoff() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="cutoff_at must use UTC"):
        build_news_receipts(
            _response(),
            query_start=date(2026, 7, 1),
            query_end=date(2026, 7, 2),
            phase=NewsEvidencePhase.PRE_EVENT,
            cutoff_at=datetime(2026, 7, 2, 17, tzinfo=timezone.utc).astimezone(
                timezone(timedelta(hours=-4))
            ),
        )


def test_news_receipt_rejects_article_exactly_at_cutoff() -> None:
    from pydantic import ValidationError

    cutoff = datetime(2026, 7, 2, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="strictly before cutoff"):
        build_news_receipts(
            _response(published_at=cutoff),
            query_start=date(2026, 7, 1),
            query_end=date(2026, 7, 2),
            phase=NewsEvidencePhase.PRE_EVENT,
            cutoff_at=cutoff,
        )
