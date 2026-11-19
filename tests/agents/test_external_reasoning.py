import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from eventlens.agents import (
    DeepSeekExternalAgentAdapter,
    ExternalReasoningError,
    ExternalReasoningErrorCode,
    run_configured_agent_route,
)
from eventlens.config import EventLensSettings
from tests.agents.test_external_handoff import _bundle, _report


def _client(responses: list[dict[str, object] | str]):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        value = responses.pop(0)
        content = value if isinstance(value, str) else json.dumps(value)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def test_deepseek_adapter_returns_validated_contract_and_safe_usage() -> None:
    from eventlens.agents import assemble_external_bundle

    source = _bundle()
    bundle = assemble_external_bundle(
        {**source.model_dump(mode="python", exclude={"content_sha256"}), "route": "deepseek"}
    )
    payload = _report(bundle).model_dump(mode="json")
    payload["usage"] = {
        "provider": "untrusted",
        "model": "untrusted",
        "prompt_version": "untrusted",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:00Z",
    }
    client, calls = _client([payload])
    adapter = DeepSeekExternalAgentAdapter(
        api_key=SecretStr("test-secret"),
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.test/v1",
        http=client,
    )

    result = asyncio.run(adapter.interpret(bundle))
    asyncio.run(client.aclose())

    assert result.usage.provider == "deepseek"
    assert result.usage.input_tokens == 100
    assert len(calls) == 1
    assert "test-secret" not in result.model_dump_json()
    request_payload = json.loads(calls[0].content)
    user_payload = json.loads(request_payload["messages"][1]["content"])
    report_schema = user_payload["report_schema"]
    assert report_schema["title"] == "ExternalAgentReport"
    assert "ExternalAgentBundle" not in json.dumps(report_schema)


def test_deepseek_adapter_allows_one_bounded_repair() -> None:
    from eventlens.agents import assemble_external_bundle

    source = _bundle()
    bundle = assemble_external_bundle(
        {**source.model_dump(mode="python", exclude={"content_sha256"}), "route": "deepseek"}
    )
    client, calls = _client(["not-json", _report(bundle).model_dump(mode="json")])
    adapter = DeepSeekExternalAgentAdapter(
        api_key=SecretStr("test-secret"),
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.test/v1",
        http=client,
    )

    assert asyncio.run(adapter.interpret(bundle)).bundle_sha256 == bundle.content_sha256
    asyncio.run(client.aclose())
    assert len(calls) == 2


def test_configured_route_persists_deepseek_result_without_fallback(tmp_path: Path) -> None:
    from eventlens.agents import assemble_external_bundle

    settings = EventLensSettings.load(
        tmp_path / "missing.env",
        environment={
            "EVENTLENS_EXTERNAL_AGENT_ENABLED": "false",
            "DEEPSEEK_API_KEY": "test-secret",
            "EVENTLENS_AGENT_MODEL": "deepseek-v4-pro",
        },
    )
    source = _bundle()
    bundle = assemble_external_bundle(
        {**source.model_dump(mode="python", exclude={"content_sha256"}), "route": "deepseek"}
    )
    client, _ = _client([_report(bundle).model_dump(mode="json")])
    adapter = DeepSeekExternalAgentAdapter(
        api_key=SecretStr("test-secret"), model="deepseek-v4-pro",
        base_url="https://api.deepseek.test/v1", http=client,
    )

    state = asyncio.run(
        run_configured_agent_route(
            bundle, settings=settings, artifact_root=tmp_path / "runs", deepseek=adapter
        )
    )
    asyncio.run(client.aclose())
    assert state.route.value == "deepseek"
    assert state.status.value == "validated"


def test_deepseek_adapter_fails_after_one_repair() -> None:
    from eventlens.agents import assemble_external_bundle

    source = _bundle()
    bundle = assemble_external_bundle(
        {**source.model_dump(mode="python", exclude={"content_sha256"}), "route": "deepseek"}
    )
    client, calls = _client(["bad", "still-bad"])
    adapter = DeepSeekExternalAgentAdapter(
        api_key=SecretStr("test-secret"), model="deepseek-v4-pro",
        base_url="https://api.deepseek.test/v1", http=client,
    )
    with pytest.raises(ExternalReasoningError) as caught:
        asyncio.run(adapter.interpret(bundle))
    asyncio.run(client.aclose())
    assert caught.value.code is ExternalReasoningErrorCode.MALFORMED_RESPONSE
    assert len(calls) == 2
