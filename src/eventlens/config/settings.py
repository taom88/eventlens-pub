"""Centralized configuration that never exposes credential values."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class CredentialStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    environment_variable: str
    configured: bool
    required_for: str
    obtain_at: str | None = None
    cost_tier: str


class MemoryBackendStatus(BaseModel):
    """Secret-free readiness summary safe for CLI/UI display."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str
    configured: bool
    detail: str


class AgentRoute(StrEnum):
    EXTERNAL = "external"
    DEEPSEEK = "deepseek"


class AgentRouteReadiness(BaseModel):
    """Secret-free route readiness for the MVP external-agent boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: AgentRoute
    ready: bool
    detail: str


class EventLensSettings(BaseModel):
    """Free-first settings loaded from one optional .env file plus environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alpha_vantage_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    massive_api_key: SecretStr | None = None
    fmp_api_key: SecretStr | None = None
    tiingo_api_key: SecretStr | None = None
    intrinio_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    moonshot_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    moonshot_base_url: str = "https://api.moonshot.ai/v1"
    ollama_base_url: str = "http://localhost:11434"

    sec_user_agent: str = ""
    enable_yfinance: bool = True
    allow_vibe_keyed_market_data: bool = False
    allow_live_fixture_options: bool = False
    alpha_vantage_daily_budget: int = Field(default=20, ge=1, le=25)
    finnhub_daily_budget: int = Field(default=50, ge=1, le=60)
    provider_cache_dir: Path = Path(".eventlens-cache/providers")
    vibe_mcp_transport: str = "stdio"
    vibe_mcp_http_url: str | None = None
    vibe_mcp_http_auth_key: SecretStr | None = None
    vibe_mcp_connection_timeout_seconds: float = Field(default=15, ge=1, le=120)
    vibe_mcp_tool_timeout_seconds: float = Field(default=60, ge=1, le=600)
    vibe_mcp_max_attempts: int = Field(default=2, ge=1, le=5)
    enable_postlock_vibe_broad_research: bool = False
    external_agent_enabled: bool = True
    vibe_artifact_dir: Path = Path(".eventlens-cache/vibe-artifacts")
    langchain_provider: str | None = None
    langchain_model_name: str | None = None
    agent_provider: str = "fixture"
    agent_model: str = "deterministic-evidence-v1"
    agent_timeout_seconds: float = Field(default=60.0, ge=5, le=300)
    agent_max_attempts: int = Field(default=3, ge=1, le=5)
    agent_max_tokens: int = Field(default=4_000, ge=500, le=16_000)
    data_mode: str = "fixture"
    fixture_dir: Path = Path("fixtures")
    workflow_artifact_dir: Path = Path(".eventlens-cache/workflows")
    memory_backend: str = "local"
    memory_sqlite_path: Path = Path(".eventlens-cache/research-memory.sqlite")
    memory_owner_id: str = "local-owner"
    memory_workspace_id: str = "local-workspace"
    supabase_url: str | None = None
    supabase_secret_key: SecretStr | None = None
    supabase_request_timeout_seconds: float = Field(default=30, ge=1, le=120)

    @field_validator("agent_provider")
    @classmethod
    def supported_agent_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {
            "fixture",
            "deepseek",
            "moonshot",
            "kimi",
            "moonshot_kimi",
        }:
            raise ValueError(
                "agent_provider must be fixture, deepseek, or moonshot/kimi"
            )
        return normalized

    @field_validator("data_mode")
    @classmethod
    def supported_data_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"fixture", "live"}:
            raise ValueError("data_mode must be fixture or live")
        return normalized

    @field_validator("memory_backend")
    @classmethod
    def supported_memory_backend(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"local", "supabase"}:
            raise ValueError("memory_backend must be local or supabase")
        return normalized

    @field_validator("vibe_mcp_transport")
    @classmethod
    def supported_vibe_mcp_transport(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"stdio", "streamable_http"}:
            raise ValueError("vibe_mcp_transport must be stdio or streamable_http")
        return normalized

    @classmethod
    def load(
        cls,
        env_file: Path | str = Path(".env"),
        *,
        environment: Mapping[str, str] | None = None,
    ) -> EventLensSettings:
        file_values = _read_env_file(Path(env_file))
        selected_environment = os.environ if environment is None else environment
        merged = {**file_values, **dict(selected_environment)}
        mapping = {
            "alpha_vantage_api_key": _nonblank(merged.get("ALPHAVANTAGE_API_KEY")),
            "finnhub_api_key": _nonblank(merged.get("FINNHUB_API_KEY")),
            "massive_api_key": _nonblank(merged.get("MASSIVE_API_KEY")),
            "fmp_api_key": _nonblank(merged.get("FMP_API_KEY")),
            "tiingo_api_key": _nonblank(merged.get("TIINGO_API_KEY")),
            "intrinio_api_key": _nonblank(merged.get("INTRINIO_API_KEY")),
            "fred_api_key": _nonblank(merged.get("FRED_API_KEY")),
            "deepseek_api_key": _nonblank(merged.get("DEEPSEEK_API_KEY")),
            "moonshot_api_key": _nonblank(merged.get("MOONSHOT_API_KEY")),
            "deepseek_base_url": merged.get(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
            ),
            "moonshot_base_url": merged.get(
                "MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1"
            ),
            "ollama_base_url": merged.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            "sec_user_agent": merged.get(
                "EVENTLENS_SEC_USER_AGENT",
                cls.model_fields["sec_user_agent"].default,
            ),
            "enable_yfinance": merged.get("EVENTLENS_ENABLE_YFINANCE", "true"),
            "allow_vibe_keyed_market_data": merged.get(
                "EVENTLENS_ALLOW_VIBE_KEYED_MARKET_DATA", "false"
            ),
            "allow_live_fixture_options": merged.get(
                "EVENTLENS_ALLOW_LIVE_FIXTURE_OPTIONS", "false"
            ),
            "alpha_vantage_daily_budget": merged.get(
                "EVENTLENS_ALPHA_VANTAGE_DAILY_BUDGET", "20"
            ),
            "finnhub_daily_budget": merged.get("EVENTLENS_FINNHUB_DAILY_BUDGET", "50"),
            "provider_cache_dir": merged.get(
                "EVENTLENS_PROVIDER_CACHE_DIR", ".eventlens-cache/providers"
            ),
            "vibe_mcp_transport": merged.get("EVENTLENS_VIBE_MCP_TRANSPORT", "stdio"),
            "vibe_mcp_http_url": _nonblank(merged.get("EVENTLENS_VIBE_MCP_HTTP_URL")),
            "vibe_mcp_http_auth_key": _nonblank(
                merged.get("EVENTLENS_VIBE_MCP_HTTP_AUTH_KEY")
            ),
            "vibe_mcp_connection_timeout_seconds": merged.get(
                "EVENTLENS_VIBE_MCP_CONNECTION_TIMEOUT_SECONDS", "15"
            ),
            "vibe_mcp_tool_timeout_seconds": merged.get(
                "EVENTLENS_VIBE_MCP_TOOL_TIMEOUT_SECONDS", "60"
            ),
            "vibe_mcp_max_attempts": merged.get("EVENTLENS_VIBE_MCP_MAX_ATTEMPTS", "2"),
            "enable_postlock_vibe_broad_research": merged.get(
                "EVENTLENS_ENABLE_POSTLOCK_VIBE_BROAD_RESEARCH", "false"
            ),
            "external_agent_enabled": merged.get(
                "EVENTLENS_EXTERNAL_AGENT_ENABLED", "true"
            ),
            "vibe_artifact_dir": merged.get(
                "EVENTLENS_VIBE_ARTIFACT_DIR", ".eventlens-cache/vibe-artifacts"
            ),
            "langchain_provider": merged.get("LANGCHAIN_PROVIDER"),
            "langchain_model_name": merged.get("LANGCHAIN_MODEL_NAME"),
            "agent_provider": merged.get(
                "EVENTLENS_AGENT_PROVIDER", merged.get("LANGCHAIN_PROVIDER", "fixture")
            ),
            "agent_model": merged.get(
                "EVENTLENS_AGENT_MODEL",
                merged.get("LANGCHAIN_MODEL_NAME", "deterministic-evidence-v1"),
            ),
            "agent_timeout_seconds": merged.get(
                "EVENTLENS_AGENT_TIMEOUT_SECONDS", "60"
            ),
            "agent_max_attempts": merged.get("EVENTLENS_AGENT_MAX_ATTEMPTS", "3"),
            "agent_max_tokens": merged.get("EVENTLENS_AGENT_MAX_TOKENS", "4000"),
            "data_mode": merged.get("EVENTLENS_DATA_MODE", "fixture"),
            "fixture_dir": merged.get("EVENTLENS_FIXTURE_DIR", "fixtures"),
            "workflow_artifact_dir": merged.get(
                "EVENTLENS_WORKFLOW_ARTIFACT_DIR", ".eventlens-cache/workflows"
            ),
            "memory_backend": merged.get("EVENTLENS_MEMORY_BACKEND", "local"),
            "memory_sqlite_path": merged.get(
                "EVENTLENS_MEMORY_SQLITE_PATH",
                ".eventlens-cache/research-memory.sqlite",
            ),
            "memory_owner_id": merged.get("EVENTLENS_MEMORY_OWNER_ID", "local-owner"),
            "memory_workspace_id": merged.get(
                "EVENTLENS_MEMORY_WORKSPACE_ID", "local-workspace"
            ),
            "supabase_url": _nonblank(merged.get("EVENTLENS_SUPABASE_URL")),
            "supabase_secret_key": _nonblank(
                merged.get("EVENTLENS_SUPABASE_SECRET_KEY")
            ),
            "supabase_request_timeout_seconds": merged.get(
                "EVENTLENS_SUPABASE_REQUEST_TIMEOUT_SECONDS", "30"
            ),
        }
        return cls.model_validate(mapping)

    def memory_backend_status(self) -> MemoryBackendStatus:
        if self.memory_backend == "local":
            return MemoryBackendStatus(
                backend="local",
                configured=True,
                detail="local SQLite research memory configured",
            )
        configured = bool(self.supabase_url and self.supabase_secret_key)
        return MemoryBackendStatus(
            backend="supabase",
            configured=configured,
            detail=(
                "hosted Supabase research memory configured"
                if configured
                else "Supabase URL or server secret key is missing"
            ),
        )

    def vibe_mcp_connection_settings(self):
        """Build the isolated MCP transport settings without exposing secrets."""

        from eventlens.research_engines.vibe_evidence import (
            VibeMcpConnectionSettings,
            VibeMcpTransport,
        )

        return VibeMcpConnectionSettings(
            transport=VibeMcpTransport(self.vibe_mcp_transport),
            http_url=self.vibe_mcp_http_url,
            http_auth_key=self.vibe_mcp_http_auth_key,
            connection_timeout_seconds=self.vibe_mcp_connection_timeout_seconds,
            tool_timeout_seconds=self.vibe_mcp_tool_timeout_seconds,
            max_attempts=self.vibe_mcp_max_attempts,
        )

    def credential_statuses(self) -> tuple[CredentialStatus, ...]:
        from eventlens.providers.sec_edgar import valid_sec_user_agent

        return (
            CredentialStatus(
                provider="Alpha Vantage",
                environment_variable="ALPHAVANTAGE_API_KEY",
                configured=self.alpha_vantage_api_key is not None,
                required_for="transcripts and earnings-history context",
                obtain_at="https://www.alphavantage.co/support/#api-key",
                cost_tier="free key; premium required for historical options/full adjusted history",
            ),
            CredentialStatus(
                provider="Finnhub",
                environment_variable="FINNHUB_API_KEY",
                configured=self.finnhub_api_key is not None,
                required_for="earnings-calendar and earnings-surprise cross-checks",
                obtain_at="https://finnhub.io/register",
                cost_tier=(
                    "free: 60 calls/minute, one-month US earnings calendar, four EPS surprises; "
                    "candles/transcripts are paid"
                ),
            ),
            CredentialStatus(
                provider="SEC EDGAR identity",
                environment_variable="EVENTLENS_SEC_USER_AGENT",
                configured=valid_sec_user_agent(self.sec_user_agent),
                required_for="no-key SEC filing evidence and fair-access compliance",
                obtain_at=None,
                cost_tier="free; use a real monitored contact email",
            ),
            CredentialStatus(
                provider="Massive",
                environment_variable="MASSIVE_API_KEY",
                configured=self.massive_api_key is not None,
                required_for="ticker-news metadata and provider-native sentiment labels",
                obtain_at="https://massive.com/dashboard/keys",
                cost_tier="free Stocks Basic: hourly news and two years of history",
            ),
            CredentialStatus(
                provider="DeepSeek",
                environment_variable="DEEPSEEK_API_KEY",
                configured=self.deepseek_api_key is not None,
                required_for="direct EventLens agent and optional Vibe-Trading reasoning",
                obtain_at="https://platform.deepseek.com/api_keys",
                cost_tier="optional pay-as-you-go LLM",
            ),
            CredentialStatus(
                provider="Moonshot/Kimi",
                environment_variable="MOONSHOT_API_KEY",
                configured=self.moonshot_api_key is not None,
                required_for="alternative direct EventLens agent and Vibe reasoning",
                obtain_at="https://platform.moonshot.ai/console/api-keys",
                cost_tier="optional pay-as-you-go LLM",
            ),
        )

    def vibe_environment(self) -> dict[str, str]:
        """Return only explicitly configured Vibe variables, never status output."""

        output: dict[str, str] = {}
        if self.langchain_provider:
            output["LANGCHAIN_PROVIDER"] = self.langchain_provider
        if self.langchain_model_name:
            output["LANGCHAIN_MODEL_NAME"] = self.langchain_model_name
        output["DEEPSEEK_BASE_URL"] = self.deepseek_base_url
        output["MOONSHOT_BASE_URL"] = self.moonshot_base_url
        output["OLLAMA_BASE_URL"] = self.ollama_base_url
        for name, secret in (
            ("DEEPSEEK_API_KEY", self.deepseek_api_key),
            ("MOONSHOT_API_KEY", self.moonshot_api_key),
            ("FRED_API_KEY", self.fred_api_key),
        ):
            if secret is not None:
                output[name] = secret.get_secret_value()
        if self.allow_vibe_keyed_market_data:
            for name, secret in (
                ("FINNHUB_API_KEY", self.finnhub_api_key),
                ("ALPHAVANTAGE_API_KEY", self.alpha_vantage_api_key),
                ("TIINGO_API_KEY", self.tiingo_api_key),
                ("FMP_API_KEY", self.fmp_api_key),
            ):
                if secret is not None:
                    output[name] = secret.get_secret_value()
        from eventlens.providers.sec_edgar import valid_sec_user_agent

        if valid_sec_user_agent(self.sec_user_agent):
            output["VIBE_TRADING_SEC_UA"] = self.sec_user_agent
        output["VIBE_TRADING_DATA_CACHE"] = "1"
        return output

    def vibe_reasoning_readiness(self) -> tuple[bool, str]:
        """Explain whether the selected Vibe reasoning provider can authenticate."""

        provider = (self.langchain_provider or "").casefold()
        if not provider:
            return False, "LANGCHAIN_PROVIDER is not configured"
        if provider == "deepseek":
            return (
                self.deepseek_api_key is not None,
                "DeepSeek key configured"
                if self.deepseek_api_key is not None
                else "DEEPSEEK_API_KEY is missing",
            )
        if provider == "moonshot":
            return (
                self.moonshot_api_key is not None,
                "Moonshot/Kimi key configured"
                if self.moonshot_api_key is not None
                else "MOONSHOT_API_KEY is missing",
            )
        if provider == "ollama":
            return (
                True,
                "Ollama selected; local service availability still requires preflight",
            )
        return (
            False,
            f"EventLens has no credential readiness rule for provider {provider}",
        )

    def agent_reasoning_readiness(self) -> tuple[bool, str]:
        """Explain whether the selected direct report/follow-up agent is runnable."""

        if self.agent_provider == "fixture":
            return True, "deterministic fixture agent selected; no LLM key required"
        if self.agent_provider == "deepseek":
            return (
                self.deepseek_api_key is not None,
                "DeepSeek key configured"
                if self.deepseek_api_key is not None
                else "DEEPSEEK_API_KEY is missing",
            )
        return (
            self.moonshot_api_key is not None,
            "Moonshot/Kimi key configured"
            if self.moonshot_api_key is not None
            else "MOONSHOT_API_KEY is missing",
        )

    def agent_route(self) -> AgentRoute:
        """Select the R1 route solely from the explicit external-agent flag."""
        return (
            AgentRoute.EXTERNAL if self.external_agent_enabled else AgentRoute.DEEPSEEK
        )

    def agent_route_readiness(self) -> AgentRouteReadiness:
        route = self.agent_route()
        if route is AgentRoute.EXTERNAL:
            return AgentRouteReadiness(
                route=route,
                ready=True,
                detail="external bundle export/import route enabled; no model key required",
            )
        configured = self.deepseek_api_key is not None
        return AgentRouteReadiness(
            route=route,
            ready=configured,
            detail="DeepSeek key configured"
            if configured
            else "DEEPSEEK_API_KEY is missing",
        )


def _nonblank(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _read_env_file(path: Path) -> dict[str, str]:
    """Read the simple KEY=VALUE subset used by EventLens's checked-in template."""

    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values
