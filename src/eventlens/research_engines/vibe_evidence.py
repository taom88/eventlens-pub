"""Typed MCP client boundary for official Vibe-Trading evidence tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from eventlens.research_engines.models import ArtifactReference, InformationBoundary


PRE_LOCK_EVIDENCE_TOOLS = frozenset(
    {
        "get_market_data",
        "get_stock_news",
        "get_sec_filings",
        "get_financial_statements",
        "get_stock_profile",
        "get_research_reports",
    }
)
BLOCKED_TOOL_NAMES = frozenset(
    {
        "write_file",
        "run_swarm",
        "read_url",
        "web_search",
        "read_document",
        "read_file",
    }
)
# No additional post-lock MCP tool has yet passed EventLens's read-only
# capability review. The feature flag is therefore necessary but not
# sufficient: newly discovered tools remain unavailable until added here.
REVIEWED_POST_LOCK_READ_ONLY_TOOLS = PRE_LOCK_EVIDENCE_TOOLS
_CANONICAL_KEYS = frozenset(
    {
        "expected_move",
        "implied_move",
        "realized_move",
        "realization_ratio",
        "event_date",
        "event_release_at",
        "reference_trading_date",
        "post_event_trading_dates",
        "locked_hypothesis",
    }
)
_TEMPORAL_KEYS = frozenset(
    {
        "date",
        "datetime",
        "trade_date",
        "published_at",
        "published",
        "filing_date",
        "accepted_at",
        "timestamp",
    }
)

# These are the *installed* 0.1.11 MCP input contracts captured during Phase 0
# (rather than guessed from the upstream README).  They deliberately describe
# only the contract EventLens calls.  A changed required field, type, or closed
# object is a compatibility failure, not something we silently adapt to.
REQUIRED_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_market_data": {
        "required": ("codes", "start_date", "end_date"),
        "properties": {"codes": "array", "start_date": "string", "end_date": "string", "source": "string", "interval": "string", "max_rows": "integer"},
    },
    "get_stock_news": {"required": (), "properties": {"code": None, "scope": "string", "limit": "integer"}},
    "get_sec_filings": {"required": ("ticker",), "properties": {"ticker": "string", "form": None, "metric": None, "limit": "integer"}},
    "get_financial_statements": {"required": ("code",), "properties": {"code": "string", "statement": "string", "period": "string"}},
    "get_stock_profile": {"required": ("ticker",), "properties": {"ticker": "string", "sections": None}},
    "get_research_reports": {"required": ("code",), "properties": {"code": "string", "limit": "integer"}},
}


class VibeMcpTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class VibeMcpConnectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: VibeMcpTransport = VibeMcpTransport.STDIO
    executable: str = "vibe-trading-mcp"
    http_url: str | None = None
    http_auth_key: SecretStr | None = None
    connection_timeout_seconds: float = Field(default=15, ge=1, le=120)
    tool_timeout_seconds: float = Field(default=60, ge=1, le=600)
    max_attempts: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def transport_fields_are_safe(self) -> "VibeMcpConnectionSettings":
        if self.transport is VibeMcpTransport.STDIO:
            if self.http_url is not None or self.http_auth_key is not None:
                raise ValueError("stdio MCP cannot use HTTP URL or authentication")
            return self
        if not self.http_url:
            raise ValueError("streamable HTTP MCP requires a URL")
        parsed = urlsplit(self.http_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP HTTP URL must be an absolute HTTP(S) URL")
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if not loopback and parsed.scheme != "https":
            raise ValueError("non-loopback MCP HTTP requires HTTPS")
        if not loopback and self.http_auth_key is None:
            raise ValueError("non-loopback MCP HTTP requires an authentication key")
        return self


class VibeMcpEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    session_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    information_cutoff_at: datetime
    idempotency_key: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]
    information_boundary: InformationBoundary = InformationBoundary.PRE_LOCK

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()

    @field_validator("information_cutoff_at")
    @classmethod
    def normalize_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("information cutoff must be timezone-aware")
        return value.astimezone(timezone.utc)


class VibeMcpEvidenceResult(BaseModel):
    """Supporting evidence only; deliberately contains no canonical metric fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    session_id: str
    event_id: str
    ticker: str
    idempotency_key: str
    provider: str = "HKUDS/Vibe-Trading"
    tool_name: str
    arguments: dict[str, Any]
    transport: VibeMcpTransport
    origin: str = "live"
    discovered_tools: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
    payload: dict[str, Any] | list[Any] | None = None
    evidence_items: tuple["VibeEvidenceItem", ...] = ()
    evidence_gaps: tuple[str, ...] = ()
    raw_artifact: ArtifactReference | None = None
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = Field(ge=0)


class VibeEvidenceItem(BaseModel):
    """A normalized, passage-addressable supporting evidence record."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1)
    passage_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    text: str = Field(min_length=1)
    available_at: datetime | None = None
    source_identifier: str | None = None

    @field_validator("available_at")
    @classmethod
    def aware_available_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class VibeMcpPreflight(BaseModel):
    """Secret-safe transport and compatibility result for ``vibe-check``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    transport: VibeMcpTransport
    upstream_version: str | None = None
    discovered_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = tuple(sorted(PRE_LOCK_EVIDENCE_TOOLS))
    missing_tools: tuple[str, ...] = ()
    schema_mismatches: tuple[str, ...] = ()
    compatible: bool = False
    reason: str | None = None


DiscoverTools = Callable[[float], Awaitable[tuple[str, ...]]]
CallTool = Callable[[str, dict[str, Any], float], Awaitable[str]]


class VibeMcpEvidenceClient:
    """MCP initialize/discover/call client with fail-closed EventLens policy."""

    def __init__(
        self,
        settings: VibeMcpConnectionSettings,
        *,
        artifact_dir: Path,
        discover_tools: DiscoverTools | None = None,
        call_tool: CallTool | None = None,
        enable_postlock_broad_research: bool = False,
    ) -> None:
        if (discover_tools is None) != (call_tool is None):
            raise ValueError("fake MCP discovery and call functions must be supplied together")
        self.settings = settings
        self._artifact_dir = artifact_dir.resolve()
        self._discover = discover_tools
        self._call = call_tool
        self._enable_postlock_broad_research = enable_postlock_broad_research
        self._results: dict[str, tuple[VibeMcpEvidenceRequest, VibeMcpEvidenceResult]] = {}

    async def discover_tools(self) -> tuple[str, ...]:
        if self._discover is not None:
            return tuple(sorted(set(await self._discover(self.settings.connection_timeout_seconds))))
        contracts = await self._official_session(discover_only=True)
        assert isinstance(contracts, dict)
        return tuple(sorted(contracts))

    async def preflight(self) -> VibeMcpPreflight:
        """Exercise the configured transport, MCP initialization, and schemas.

        This intentionally does not call a data tool, so it consumes neither a
        market-data quota nor an EventLens reasoning credential.
        """

        version = _installed_vibe_version()
        try:
            if self._discover is not None:
                names = await asyncio.wait_for(
                    self.discover_tools(), self.settings.connection_timeout_seconds
                )
                # Injected test callers expose names only.  They are useful for
                # collection tests but cannot establish live schema compatibility.
                contracts: dict[str, dict[str, Any]] = {}
            else:
                value = await asyncio.wait_for(
                    self._official_session(discover_only=True),
                    self.settings.connection_timeout_seconds,
                )
                assert isinstance(value, dict)
                contracts = value
                names = tuple(sorted(contracts))
        except (asyncio.TimeoutError, OSError, RuntimeError, ValueError, ImportError):
            return VibeMcpPreflight(
                available=False,
                transport=self.settings.transport,
                upstream_version=version,
                reason="configured Vibe MCP transport could not initialize and discover tools",
            )
        missing = tuple(sorted(PRE_LOCK_EVIDENCE_TOOLS - set(names)))
        mismatches = (
            () if self._discover is not None else _schema_mismatches(contracts)
        )
        return VibeMcpPreflight(
            available=True,
            transport=self.settings.transport,
            upstream_version=version,
            discovered_tools=names,
            missing_tools=missing,
            schema_mismatches=mismatches,
            compatible=not missing and not mismatches,
            reason=(
                None
                if not missing and not mismatches
                else "configured MCP tool surface is not compatible with EventLens"
            ),
        )

    async def collect(self, request: VibeMcpEvidenceRequest) -> VibeMcpEvidenceResult:
        existing = self._results.get(request.idempotency_key)
        if existing is not None:
            previous, result = existing
            if previous != request:
                return self._failure(request, (), "idempotency_conflict", "Idempotency key was reused.", 0)
            return result
        try:
            durable = self._read_durable(request)
        except RuntimeError:
            return self._failure(
                request, (), "artifact_integrity",
                "Durable MCP evidence cache failed integrity validation.", 0
            )
        if durable is not None:
            self._results[request.idempotency_key] = (request, durable)
            return durable
        policy_error = _policy_error(
            request, enable_postlock_broad_research=self._enable_postlock_broad_research
        )
        if policy_error is not None:
            result = self._failure(request, (), "policy_violation", policy_error, 0)
            self._results[request.idempotency_key] = (request, result)
            return result

        started = datetime.now(timezone.utc)
        discovered: tuple[str, ...] = ()
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                discovered = await asyncio.wait_for(
                    self.discover_tools(), self.settings.connection_timeout_seconds
                )
                if request.tool_name not in discovered:
                    result = self._failure(
                        request, discovered, "tool_unavailable",
                        "Required allowlisted MCP tool was not discovered.", attempt, started
                    )
                    break
                raw_text = await asyncio.wait_for(
                    self._invoke(request.tool_name, request.arguments),
                    self.settings.tool_timeout_seconds,
                )
                payload = json.loads(raw_text)
                if not isinstance(payload, (dict, list)):
                    raise ValueError("MCP evidence payload must be an object or array")
                _validate_tool_payload(request, payload)
                _reject_post_cutoff(payload, request.information_cutoff_at)
                canonical = sorted(_find_keys(payload, _CANONICAL_KEYS))
                raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                artifact = self._store(raw)
                result = VibeMcpEvidenceResult(
                    status="succeeded",
                    session_id=request.session_id,
                    event_id=request.event_id,
                    ticker=request.ticker,
                    idempotency_key=request.idempotency_key,
                    tool_name=request.tool_name,
                    arguments=request.arguments,
                    transport=self.settings.transport,
                    discovered_tools=discovered,
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    payload=payload,
                    evidence_items=_normalize_evidence_items(request, payload),
                    evidence_gaps=_evidence_gaps(request, payload),
                    raw_artifact=artifact,
                    warnings=(
                        (
                            "Payload contains canonical-looking keys that remain "
                            f"supporting evidence only: {', '.join(canonical)}"
                        ),
                    ) if canonical else (),
                    attempts=attempt,
                )
                break
            except asyncio.TimeoutError:
                if attempt == self.settings.max_attempts:
                    result = self._failure(
                        request, discovered, "timeout", "MCP initialization or tool call timed out.",
                        attempt, started
                    )
                    break
            except httpx.HTTPStatusError as error:
                code = "authentication" if error.response.status_code in {401, 403} else "transport"
                result = self._failure(
                    request, discovered, code, "MCP HTTP server rejected the request.",
                    attempt, started
                )
                break
            except (OSError, RuntimeError, ValueError, ImportError, json.JSONDecodeError):
                if attempt == self.settings.max_attempts:
                    result = self._failure(
                        request, discovered, "malformed_or_transport",
                        "MCP server failed or returned invalid/cutoff-violating evidence.",
                        attempt, started
                    )
                    break
        self._results[request.idempotency_key] = (request, result)
        if result.status == "succeeded":
            self._write_durable(request, result)
        return result

    async def _invoke(self, name: str, arguments: dict[str, Any]) -> str:
        if self._call is not None:
            return await self._call(name, arguments, self.settings.tool_timeout_seconds)
        value = await self._official_session(
            discover_only=False, tool_name=name, arguments=arguments
        )
        assert isinstance(value, str)
        return value

    async def _official_session(
        self,
        *,
        discover_only: bool,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]] | str:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        if self.settings.transport is VibeMcpTransport.STDIO:
            executable = _resolve_executable(self.settings.executable)
            if executable is None:
                raise RuntimeError("Vibe MCP executable is unavailable")
            safe_env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
            }
            with tempfile.TemporaryDirectory(prefix="eventlens-vibe-mcp-") as home:
                safe_env.update({"HOME": home, "XDG_CACHE_HOME": f"{home}/cache", "XDG_CONFIG_HOME": f"{home}/config", "XDG_DATA_HOME": f"{home}/data"})
                params = StdioServerParameters(command=executable, args=["--transport", "stdio"], env=safe_env)
                async with stdio_client(params) as (read, write):
                    return await _use_session(
                        ClientSession(read, write), discover_only, tool_name, arguments,
                        self.settings.connection_timeout_seconds,
                        self.settings.tool_timeout_seconds,
                    )

        headers = {}
        if self.settings.http_auth_key is not None:
            headers["Authorization"] = f"Bearer {self.settings.http_auth_key.get_secret_value()}"
        timeout = httpx.Timeout(
            self.settings.tool_timeout_seconds,
            connect=self.settings.connection_timeout_seconds,
        )
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as http:
            async with streamable_http_client(self.settings.http_url or "", http_client=http) as (read, write, _):
                return await _use_session(
                    ClientSession(read, write), discover_only, tool_name, arguments,
                    self.settings.connection_timeout_seconds,
                    self.settings.tool_timeout_seconds,
                )

    def _store(self, raw: bytes) -> ArtifactReference:
        digest = hashlib.sha256(raw).hexdigest()
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        path = (self._artifact_dir / f"{digest}.json").resolve()
        if path.parent != self._artifact_dir:
            raise ValueError("MCP artifact path escaped configured root")
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("MCP artifact checksum validation failed")
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".mcp-", dir=self._artifact_dir)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return ArtifactReference(
            reference=f"artifact://vibe-mcp-evidence/{digest}",
            media_type="application/json",
            sha256=digest,
            size_bytes=len(raw),
        )

    async def get_artifact(self, reference: ArtifactReference) -> bytes | None:
        if not reference.reference.startswith("artifact://vibe-mcp-evidence/"):
            return None
        path = (self._artifact_dir / f"{reference.sha256}.json").resolve()
        if path.parent != self._artifact_dir:
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            return None
        return content

    def _idempotency_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        directory = (self._artifact_dir / "idempotency").resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / f"{digest}.json").resolve()
        if path.parent != directory:
            raise ValueError("MCP idempotency path escaped configured root")
        return path

    def _read_durable(
        self, request: VibeMcpEvidenceRequest
    ) -> VibeMcpEvidenceResult | None:
        path = self._idempotency_path(request.idempotency_key)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            stored_request = VibeMcpEvidenceRequest.model_validate(envelope["request"])
            if stored_request != request:
                return self._failure(
                    request, (), "idempotency_conflict",
                    "Idempotency key was reused for a different MCP request.", 0
                )
            result = VibeMcpEvidenceResult.model_validate(envelope["result"])
            if result.raw_artifact is None:
                raise ValueError("successful cached result omitted its raw artifact")
            artifact_path = self._artifact_dir / f"{result.raw_artifact.sha256}.json"
            raw = artifact_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != result.raw_artifact.sha256:
                raise ValueError("cached MCP artifact checksum mismatch")
            return result.model_copy(update={"origin": "cache"})
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("durable MCP evidence cache is invalid") from error

    def _write_durable(
        self,
        request: VibeMcpEvidenceRequest,
        result: VibeMcpEvidenceResult,
    ) -> None:
        path = self._idempotency_path(request.idempotency_key)
        content = (
            json.dumps(
                {
                    "request": request.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        descriptor, temporary_name = tempfile.mkstemp(prefix=".idempotency-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _failure(
        self,
        request: VibeMcpEvidenceRequest,
        discovered: tuple[str, ...],
        code: str,
        message: str,
        attempts: int,
        started: datetime | None = None,
    ) -> VibeMcpEvidenceResult:
        return VibeMcpEvidenceResult(
            status="failed",
            session_id=request.session_id,
            event_id=request.event_id,
            ticker=request.ticker,
            idempotency_key=request.idempotency_key,
            tool_name=request.tool_name,
            arguments=request.arguments,
            transport=self.settings.transport,
            discovered_tools=discovered,
            started_at=started or datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            error_code=code,
            error_message=message,
            attempts=attempts,
        )


async def _use_session(
    session: Any,
    discover_only: bool,
    tool_name: str | None,
    arguments: dict[str, Any] | None,
    init_timeout: float,
    tool_timeout: float,
) -> dict[str, dict[str, Any]] | str:
    async with session:
        await asyncio.wait_for(session.initialize(), init_timeout)
        tools = await asyncio.wait_for(session.list_tools(), init_timeout)
        contracts = {
            tool.name: dict(getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {})
            for tool in tools.tools
        }
        if discover_only:
            return contracts
        result = await session.call_tool(
            tool_name or "", arguments or {},
            read_timeout_seconds=timedelta(seconds=tool_timeout),
        )
        if result.isError:
            raise RuntimeError("Vibe MCP tool returned an error")
        parts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
        if not parts:
            raise ValueError("Vibe MCP tool returned no text content")
        return "\n".join(parts)


def _policy_error(
    request: VibeMcpEvidenceRequest, *, enable_postlock_broad_research: bool = False
) -> str | None:
    name = request.tool_name
    if _is_permanently_blocked(name):
        return "Mutation, trading, swarm, shell, file, and unrestricted retrieval tools are blocked."
    if request.information_boundary is InformationBoundary.PRE_LOCK:
        if name not in PRE_LOCK_EVIDENCE_TOOLS:
            return "Tool is not in the EventLens pre-lock evidence allowlist."
    elif name not in PRE_LOCK_EVIDENCE_TOOLS:
        if not enable_postlock_broad_research:
            return (
                "Broad post-lock Vibe research is disabled; set "
                "EVENTLENS_ENABLE_POSTLOCK_VIBE_BROAD_RESEARCH=true to permit it."
            )
        if name not in REVIEWED_POST_LOCK_READ_ONLY_TOOLS:
            return "Post-lock tool has not passed the EventLens read-only capability review."
    return None


def _is_permanently_blocked(name: str) -> bool:
    normalized = name.casefold()
    compact = "".join(character for character in normalized if character.isalnum())
    tokens = {
        token
        for token in normalized.replace("-", "_").split("_")
        if token
    }
    return (
        normalized in BLOCKED_TOOL_NAMES
        or normalized.startswith(("trading_", "broker_", "order_", "write_", "delete_", "mutate_"))
        or bool(
            tokens
            & {
                "buy",
                "sell",
                "trade",
                "trading",
                "order",
                "broker",
                "execute",
                "execution",
                "submit",
                "position",
            }
        )
        or "shell" in normalized
        or any(
            marker in compact
            for marker in (
                "placeorder",
                "submitorder",
                "executeorder",
                "executetrade",
                "buy",
                "sell",
                "broker",
                "trading",
            )
        )
        or normalized in {"read_file", "write_file", "delete_file", "read_document", "write_document"}
        or normalized in {"read_url", "web_search", "run_swarm"}
    )


def _installed_vibe_version() -> str | None:
    try:
        return metadata.version("vibe-trading-ai")
    except metadata.PackageNotFoundError:
        return None


def _schema_mismatches(contracts: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    mismatches: list[str] = []
    for name, expected in REQUIRED_TOOL_SCHEMAS.items():
        schema = contracts.get(name)
        if schema is None:
            continue
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            mismatches.append(f"{name}: object contract changed")
            continue
        required = tuple(schema.get("required", ()))
        if required != expected["required"]:
            mismatches.append(f"{name}: required arguments changed")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            mismatches.append(f"{name}: properties are unavailable")
            continue
        for argument, expected_type in expected["properties"].items():
            field = properties.get(argument)
            if not isinstance(field, dict):
                mismatches.append(f"{name}: missing {argument}")
            elif expected_type is not None and field.get("type") != expected_type:
                mismatches.append(f"{name}: {argument} type changed")
    return tuple(mismatches)


def _validate_tool_payload(request: VibeMcpEvidenceRequest, payload: Any) -> None:
    """Fail closed on tool-specific malformed or wrong-symbol responses.

    Vibe 0.1.11 has no stable output JSON Schema.  The checks below are based
    on its published envelopes and documented stable identifiers; fields not
    supplied by a tool are not invented by EventLens.
    """

    if isinstance(payload, dict) and payload.get("status") == "error":
        raise ValueError("Vibe MCP returned an error envelope")
    expected = _symbol_variants(request.ticker)
    if request.tool_name == "get_market_data":
        code = str((request.arguments.get("codes") or [""])[0]).upper()
        if not isinstance(payload, dict) or code not in payload:
            raise ValueError("market-data response omitted requested symbol")
        rows = payload[code]
        if isinstance(rows, dict):
            rows = rows.get("data")
        if not isinstance(rows, list) or not rows:
            raise ValueError("market-data response contained no requested rows")
        _verify_symbol_fields(payload, expected, require_observed=False)
        return
    _verify_symbol_fields(payload, expected)
    if request.tool_name == "get_sec_filings":
        _require_records_or_explicit_empty(payload, "SEC filings")
    elif request.tool_name in {"get_stock_news", "get_research_reports"}:
        _require_records_or_explicit_empty(payload, "news or research reports")
    elif request.tool_name == "get_financial_statements":
        _require_records_or_explicit_empty(payload, "financial statements")
    elif request.tool_name == "get_stock_profile" and not isinstance(payload, dict):
        raise ValueError("stock profile response must be an object")


def _symbol_variants(ticker: str) -> set[str]:
    raw = ticker.strip().upper()
    bare = raw.removesuffix(".US")
    return {raw, bare, f"{bare}.US"}


def _verify_symbol_fields(
    payload: Any, expected: set[str], *, require_observed: bool = True
) -> None:
    observed = {
        str(value).strip().upper()
        for key, value in _walk_items(payload)
        if key.casefold() in {"symbol", "ticker", "code"} and isinstance(value, str)
    }
    if any(item not in expected for item in observed):
        raise ValueError("MCP evidence symbol does not match the requested ticker")
    records = _record_dicts(payload)
    if require_observed and records and not observed:
        raise ValueError("MCP evidence records do not expose a verifiable ticker")


def _require_records_or_explicit_empty(payload: Any, label: str) -> None:
    if isinstance(payload, list):
        return
    if not isinstance(payload, dict):
        raise ValueError(f"{label} response has an unsupported shape")
    if payload.get("status") == "ok":
        return
    for key in ("data", "items", "results", "reports", "filings", "records"):
        if isinstance(payload.get(key), list):
            return
    # Some official tools return a named, flat record object (for example a
    # stock profile).  Those are handled by their own branch above.
    raise ValueError(f"{label} response omitted records or an official success envelope")


def _evidence_gaps(request: VibeMcpEvidenceRequest, payload: Any) -> tuple[str, ...]:
    """Keep lack of usable upstream evidence visible to the reasoning layer."""

    if request.tool_name == "get_research_reports" and request.ticker.upper().endswith(".US"):
        return ("Vibe get_research_reports supports mainland A-share symbols only.",)
    if isinstance(payload, dict) and payload.get("status") == "ok" and not _record_dicts(payload):
        return (f"{request.tool_name} returned an explicit success envelope with no citable records.",)
    return ()


def _normalize_evidence_items(
    request: VibeMcpEvidenceRequest, payload: Any
) -> tuple[VibeEvidenceItem, ...]:
    """Convert validated tool records into small stable passages.

    Raw payloads remain checksum-addressed artifacts.  These passages are the
    only portion eligible for an LLM prompt, preventing a Vibe response from
    becoming canonical EventLens state.
    """

    records = _record_dicts(payload)
    items: list[VibeEvidenceItem] = []
    for index, record in enumerate(records):
        identifier = _stable_identifier(record)
        if _requires_stable_identifier(request.tool_name) and identifier is None:
            # The relevant upstream tool advertises stable IDs when it has a
            # record.  A record without one is not citable rather than being
            # assigned a misleading pseudo-source.
            continue
        digest = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()[:16]
        source_id = f"vibe:{request.tool_name}:{identifier or digest}"
        passage_id = f"{request.tool_name}:{index}:{digest[:10]}"
        text = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
        available_at = _record_timestamp(record)
        if available_at is None:
            # An undated record cannot be proven to respect an historical
            # information boundary, so it remains only in the raw artifact.
            continue
        items.append(
            VibeEvidenceItem(
                source_id=source_id,
                passage_id=passage_id,
                tool_name=request.tool_name,
                text=text,
                available_at=available_at,
                source_identifier=identifier,
            )
        )
    return tuple(items)


def _record_dicts(payload: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(payload, list):
        return tuple(item for item in payload if isinstance(item, dict))
    if not isinstance(payload, dict):
        return ()
    for key in ("data", "items", "results", "reports", "filings", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return tuple(item for item in value if isinstance(item, dict))
    # Market data is keyed by requested code; its rows are citable observations.
    rows: list[dict[str, Any]] = []
    for value in payload.values():
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict) and isinstance(value.get("data"), list):
            rows.extend(item for item in value["data"] if isinstance(item, dict))
    return tuple(rows)


def _stable_identifier(record: dict[str, Any]) -> str | None:
    for key in (
        "id", "url", "link", "accession_number", "accessionNumber", "report_id",
        "uuid", "guid", "primaryDocument",
    ):
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _requires_stable_identifier(tool_name: str) -> bool:
    # SEC filings document accession numbers; research/news feeds generally
    # expose a link or provider identifier when an item is addressable.
    return tool_name in {"get_sec_filings", "get_stock_news", "get_research_reports"}


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    for key in _TEMPORAL_KEYS:
        value = record.get(key)
        if value is None:
            continue
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _reject_post_cutoff(payload: Any, cutoff: datetime) -> None:
    for key, value in _walk_items(payload):
        if key.casefold() not in _TEMPORAL_KEYS or value is None:
            continue
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text[:10] + "T00:00:00+00:00")
            except ValueError:
                raise ValueError("MCP evidence contains an unparseable timestamp")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.astimezone(timezone.utc) > cutoff:
            raise ValueError("MCP evidence contains a timestamp after the information cutoff")


def _walk_items(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_items(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_items(item)


def _find_keys(value: Any, names: frozenset[str]) -> set[str]:
    return {key for key, _ in _walk_items(value) if key.casefold() in names}


def _resolve_executable(executable: str) -> str | None:
    resolved = shutil.which(executable)
    if resolved is not None:
        return resolved
    sibling = Path(sys.executable).parent / executable
    return str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None
