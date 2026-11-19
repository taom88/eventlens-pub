from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from eventlens.research_engines import (
    InformationBoundary,
    PRE_LOCK_EVIDENCE_TOOLS,
    VibeMcpConnectionSettings,
    VibeMcpEvidenceClient,
    VibeMcpEvidenceRequest,
    VibeMcpTransport,
)


def _request(**changes):
    values = {
        "session_id": "session-1",
        "event_id": "DEMO:2026Q2",
        "ticker": "DEMO",
        "information_cutoff_at": datetime(2026, 7, 25, 22, tzinfo=timezone.utc),
        "idempotency_key": "session-1:market",
        "tool_name": "get_market_data",
        "arguments": {
            "codes": ["DEMO.US"],
            "start_date": "2026-07-01",
            "end_date": "2026-07-24",
            "source": "yfinance",
        },
        "information_boundary": InformationBoundary.PRE_LOCK,
    }
    values.update(changes)
    return VibeMcpEvidenceRequest(**values)


def _fake_client(tmp_path: Path, payload: object, calls: list | None = None):
    observed = calls if calls is not None else []

    async def discover(timeout):
        observed.append(("discover", timeout))
        return tuple(PRE_LOCK_EVIDENCE_TOOLS)

    async def call(name, arguments, timeout):
        observed.append(("call", name, arguments, timeout))
        return json.dumps(payload)

    return VibeMcpEvidenceClient(
        VibeMcpConnectionSettings(),
        artifact_dir=tmp_path,
        discover_tools=discover,
        call_tool=call,
    )


def test_initialization_discovery_and_allowed_tool_preserve_provenance(
    tmp_path: Path,
) -> None:
    calls = []
    client = _fake_client(
        tmp_path,
        {"DEMO.US": [{"trade_date": "2026-07-24", "close": 595.19}]},
        calls,
    )

    result = asyncio.run(client.collect(_request()))

    assert result.status == "succeeded"
    assert result.provider == "HKUDS/Vibe-Trading"
    assert result.tool_name == "get_market_data"
    assert result.arguments == _request().arguments
    assert result.transport is VibeMcpTransport.STDIO
    assert "get_sec_filings" in result.discovered_tools
    assert calls[0][0] == "discover"
    assert calls[1][0] == "call"
    assert result.raw_artifact is not None
    artifact = tmp_path / f"{result.raw_artifact.sha256}.json"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == result.raw_artifact.sha256


def test_disallowed_tool_is_rejected_before_discovery_or_call(tmp_path: Path) -> None:
    calls = []
    client = _fake_client(tmp_path, {}, calls)

    result = asyncio.run(
        client.collect(
            _request(
                tool_name="write_file",
                arguments={"path": "/tmp/x", "content": "bad"},
                idempotency_key="blocked",
            )
        )
    )

    assert result.error_code == "policy_violation"
    assert calls == []


def test_postlock_broad_research_gate_cannot_bypass_permanent_blocks(tmp_path: Path) -> None:
    calls = []
    client = _fake_client(tmp_path, {}, calls)
    broad = _request(
        tool_name="screen_market",
        arguments={"market": "us"},
        idempotency_key="broad",
        information_boundary=InformationBoundary.POST_LOCK,
    )
    denied = asyncio.run(client.collect(broad))
    assert denied.error_code == "policy_violation"
    assert calls == []

    enabled = VibeMcpEvidenceClient(
        VibeMcpConnectionSettings(max_attempts=1),
        artifact_dir=tmp_path / "enabled",
        discover_tools=lambda timeout: _async_value(("screen_market",)),
        call_tool=lambda name, arguments, timeout: _async_value('{"status":"ok","data":[]}'),
        enable_postlock_broad_research=True,
    )
    assert asyncio.run(enabled.collect(broad)).error_code == "policy_violation"
    blocked = asyncio.run(
        enabled.collect(broad.model_copy(update={"tool_name": "write_file", "idempotency_key": "write"}))
    )
    assert blocked.error_code == "policy_violation"
    for tool_name in ("place_order", "submit_order", "execute_trade"):
        blocked = asyncio.run(
            enabled.collect(
                broad.model_copy(
                    update={"tool_name": tool_name, "idempotency_key": tool_name}
                )
            )
        )
        assert blocked.error_code == "policy_violation"


async def _async_value(value):
    return value


def test_missing_optional_mcp_is_typed_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    import builtins

    original_import = builtins.__import__
    attempted_mcp_imports = []

    def import_without_mcp(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            attempted_mcp_imports.append(name)
            raise ModuleNotFoundError("optional MCP SDK is not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_mcp)
    client = VibeMcpEvidenceClient(
        VibeMcpConnectionSettings(executable="definitely-missing-vibe-mcp"),
        artifact_dir=tmp_path,
    )

    result = asyncio.run(client.collect(_request()))

    assert result.status == "failed"
    assert result.error_code == "malformed_or_transport"
    assert attempted_mcp_imports == ["mcp"] * client.settings.max_attempts


def test_http_authentication_failure_is_secret_safe(tmp_path: Path) -> None:
    secret = "mcp-http-super-secret"

    async def discover(timeout):
        request = httpx.Request("POST", "https://mcp.example.test/mcp")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("contains no credentials", request=request, response=response)

    async def call(name, arguments, timeout):
        raise AssertionError("call must not run")

    client = VibeMcpEvidenceClient(
        VibeMcpConnectionSettings(
            transport=VibeMcpTransport.STREAMABLE_HTTP,
            http_url="https://mcp.example.test/mcp",
            http_auth_key=SecretStr(secret),
        ),
        artifact_dir=tmp_path,
        discover_tools=discover,
        call_tool=call,
    )

    result = asyncio.run(client.collect(_request()))

    assert result.error_code == "authentication"
    assert secret not in json.dumps(result.model_dump(mode="json"))
    assert not list(tmp_path.glob("*.json"))


def test_non_loopback_http_requires_https_and_auth() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        VibeMcpConnectionSettings(
            transport=VibeMcpTransport.STREAMABLE_HTTP,
            http_url="http://mcp.example.test/mcp",
            http_auth_key=SecretStr("secret"),
        )


def test_preflight_reports_schema_compatibility_without_credentials(tmp_path: Path) -> None:
    client = _fake_client(tmp_path, {}, [])
    result = asyncio.run(client.preflight())

    assert result.available is True
    assert result.compatible is True
    assert result.missing_tools == ()
    assert result.schema_mismatches == ()
    with pytest.raises(ValidationError, match="authentication"):
        VibeMcpConnectionSettings(
            transport=VibeMcpTransport.STREAMABLE_HTTP,
            http_url="https://mcp.example.test/mcp",
        )


def test_malformed_and_post_cutoff_payloads_fail_closed(tmp_path: Path) -> None:
    async def discover(timeout):
        return tuple(PRE_LOCK_EVIDENCE_TOOLS)

    async def malformed(name, arguments, timeout):
        return "not-json"

    malformed_client = VibeMcpEvidenceClient(
        VibeMcpConnectionSettings(max_attempts=1),
        artifact_dir=tmp_path / "malformed",
        discover_tools=discover,
        call_tool=malformed,
    )
    assert asyncio.run(malformed_client.collect(_request())).status == "failed"

    cutoff_client = _fake_client(
        tmp_path / "cutoff",
        {"items": [{"published_at": "2026-07-26T12:00:00Z"}]},
    )
    result = asyncio.run(
        cutoff_client.collect(
            _request(
                tool_name="get_stock_news",
                idempotency_key="post-cutoff",
                arguments={"symbol": "DEMO"},
            )
        )
    )
    assert result.status == "failed"
    assert not list((tmp_path / "cutoff").glob("*.json"))


def test_symbol_less_undated_and_mixed_symbol_records_are_not_citable(
    tmp_path: Path,
) -> None:
    base = _request(
        tool_name="get_stock_news",
        arguments={"code": "DEMO.US", "scope": "stock", "limit": 20},
    )
    payloads = (
        {"items": [{"id": "n1", "headline": "undated and symbol-less"}]},
        {
            "items": [
                {
                    "id": "n1",
                    "symbol": "DEMO",
                    "published_at": "2026-07-24T12:00:00Z",
                },
                {
                    "id": "n2",
                    "symbol": "AAPL",
                    "published_at": "2026-07-24T12:00:00Z",
                },
            ]
        },
    )
    for index, payload in enumerate(payloads):
        result = asyncio.run(
            _fake_client(tmp_path / str(index), payload).collect(
                base.model_copy(update={"idempotency_key": f"unsafe-{index}"})
            )
        )
        assert result.status == "failed"
        assert result.evidence_items == ()


def test_retry_is_idempotent_and_canonical_keys_remain_noncanonical(
    tmp_path: Path,
) -> None:
    calls = []
    client = _fake_client(
        tmp_path,
        {"DEMO.US": [{"date": "2026-07-24", "expected_move": "untrusted"}]},
        calls,
    )
    request = _request()

    first = asyncio.run(client.collect(request))
    second = asyncio.run(client.collect(request))

    assert first == second
    assert len([item for item in calls if item[0] == "call"]) == 1
    assert "expected_move" not in type(first).model_fields
    assert first.warnings and "supporting evidence only" in first.warnings[0]

    restarted_calls = []
    restarted = _fake_client(tmp_path, {"must": "not be called"}, restarted_calls)
    replayed = asyncio.run(restarted.collect(request))
    assert replayed.origin == "cache"
    assert restarted_calls == []


def test_artifact_checksum_tampering_fails_closed(tmp_path: Path) -> None:
    payload = {"DEMO.US": [{"trade_date": "2026-07-24", "close": 595.19}]}
    first = _fake_client(tmp_path, payload)
    result = asyncio.run(first.collect(_request()))
    assert result.raw_artifact is not None
    path = tmp_path / f"{result.raw_artifact.sha256}.json"
    path.write_text("tampered", encoding="utf-8")

    reopened = _fake_client(tmp_path, payload)
    repeated = asyncio.run(reopened.collect(_request()))

    assert repeated.status == "failed"


@pytest.mark.skipif(
    os.environ.get("EVENTLENS_RUN_LIVE_VIBE_MCP_SMOKE") != "1",
    reason="explicit live Vibe MCP smoke flag is not enabled",
)
def test_optional_live_stdio_discovery(tmp_path: Path) -> None:
    client = VibeMcpEvidenceClient(
        VibeMcpConnectionSettings(),
        artifact_dir=tmp_path,
    )
    discovered = asyncio.run(client.discover_tools())
    assert PRE_LOCK_EVIDENCE_TOOLS <= set(discovered)
