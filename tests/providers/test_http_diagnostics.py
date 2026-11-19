import httpx
import pytest

from eventlens.providers.http import ProviderHTTPError, _safe_response_detail


def test_http_diagnostic_is_bounded_and_redacts_query_credentials() -> None:
    response = httpx.Response(
        401,
        headers={"content-type": "text/html"},
        content=b"invalid token=super-secret-value",
        request=httpx.Request("GET", "https://example.test"),
    )
    detail = _safe_response_detail(response)
    assert "status=401" in detail
    assert "content_type=text/html" in detail
    assert "super-secret-value" not in detail
    assert "<redacted>" in detail
    assert len(detail) < 500


def test_provider_http_error_remains_safe_to_surface() -> None:
    error = ProviderHTTPError("finnhub request failed: status=429 body_sha256=" + "a" * 64)
    assert "429" in str(error)
    assert "api_key" not in str(error).lower()
