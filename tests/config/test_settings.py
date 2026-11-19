from __future__ import annotations

from pathlib import Path

import pytest

from eventlens.config import AgentRoute, EventLensSettings


def test_env_file_is_single_key_source_and_environment_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """ALPHAVANTAGE_API_KEY=alpha-file
FINNHUB_API_KEY=finn-file
MASSIVE_API_KEY=massive-file
DEEPSEEK_API_KEY=deep-file
EVENTLENS_SEC_USER_AGENT="EventLens test@eventlens.dev"
""",
        encoding="utf-8",
    )

    settings = EventLensSettings.load(
        env_file, environment={"ALPHAVANTAGE_API_KEY": "alpha-environment"}
    )

    assert settings.alpha_vantage_api_key is not None
    assert settings.alpha_vantage_api_key.get_secret_value() == "alpha-environment"
    assert settings.finnhub_api_key is not None
    massive = next(
        status
        for status in settings.credential_statuses()
        if status.environment_variable == "MASSIVE_API_KEY"
    )
    assert massive.configured is True
    assert all(status.configured for status in settings.credential_statuses()[:3])


def test_vibe_does_not_receive_metered_market_keys_without_opt_in() -> None:
    settings = EventLensSettings.model_validate(
        {
            "alpha_vantage_api_key": "alpha-secret",
            "finnhub_api_key": "finn-secret",
            "deepseek_api_key": "deep-secret",
        }
    )

    environment = settings.vibe_environment()

    assert environment["DEEPSEEK_API_KEY"] == "deep-secret"
    assert "ALPHAVANTAGE_API_KEY" not in environment
    assert "FINNHUB_API_KEY" not in environment


def test_vibe_keyed_market_data_can_be_deliberately_enabled() -> None:
    settings = EventLensSettings.model_validate(
        {
            "alpha_vantage_api_key": "alpha-secret",
            "finnhub_api_key": "finn-secret",
            "allow_vibe_keyed_market_data": True,
        }
    )

    environment = settings.vibe_environment()

    assert environment["ALPHAVANTAGE_API_KEY"] == "alpha-secret"
    assert environment["FINNHUB_API_KEY"] == "finn-secret"


def test_explicit_empty_environment_does_not_fall_back_to_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "process-secret")

    settings = EventLensSettings.load(tmp_path / "missing.env", environment={})

    assert settings.alpha_vantage_api_key is None


def test_settings_loads_vibe_mcp_transport_without_exposing_auth(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """EVENTLENS_VIBE_MCP_TRANSPORT=streamable_http
EVENTLENS_VIBE_MCP_HTTP_URL=https://mcp.example.test/mcp
EVENTLENS_VIBE_MCP_HTTP_AUTH_KEY=transport-secret
EVENTLENS_VIBE_MCP_CONNECTION_TIMEOUT_SECONDS=9
EVENTLENS_VIBE_MCP_TOOL_TIMEOUT_SECONDS=45
EVENTLENS_VIBE_MCP_MAX_ATTEMPTS=3
""",
        encoding="utf-8",
    )

    settings = EventLensSettings.load(env_file, environment={})

    assert settings.vibe_mcp_transport == "streamable_http"
    assert settings.vibe_mcp_http_url == "https://mcp.example.test/mcp"
    assert settings.vibe_mcp_http_auth_key is not None
    assert settings.vibe_mcp_http_auth_key.get_secret_value() == "transport-secret"
    assert settings.vibe_mcp_connection_timeout_seconds == 9
    assert settings.vibe_mcp_tool_timeout_seconds == 45
    assert settings.vibe_mcp_max_attempts == 3
    assert "transport-secret" not in str(settings)


def test_deepseek_agent_settings_default_from_langchain_and_can_override(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """LANGCHAIN_PROVIDER=deepseek
LANGCHAIN_MODEL_NAME=deepseek-v4-pro
DEEPSEEK_API_KEY=deep-secret
""",
        encoding="utf-8",
    )

    settings = EventLensSettings.load(env_file, environment={})

    assert settings.agent_provider == "deepseek"
    assert settings.agent_model == "deepseek-v4-pro"
    assert settings.agent_reasoning_readiness() == (True, "DeepSeek key configured")
    assert "deep-secret" not in str(settings)

    overridden = EventLensSettings.load(
        env_file,
        environment={
            "EVENTLENS_AGENT_PROVIDER": "fixture",
            "EVENTLENS_AGENT_MODEL": "deterministic-evidence-v1",
        },
    )

    assert overridden.agent_provider == "fixture"
    assert overridden.agent_model == "deterministic-evidence-v1"


def test_external_agent_mode_defaults_on_and_environment_overrides(
    tmp_path: Path,
) -> None:
    assert (
        EventLensSettings.load(
            tmp_path / "missing.env", environment={}
        ).external_agent_enabled
        is True
    )
    env_file = tmp_path / ".env"
    env_file.write_text("EVENTLENS_EXTERNAL_AGENT_ENABLED=false\n", encoding="utf-8")
    settings = EventLensSettings.load(env_file, environment={})
    assert settings.external_agent_enabled is False
    overridden = EventLensSettings.load(
        env_file, environment={"EVENTLENS_EXTERNAL_AGENT_ENABLED": "true"}
    )
    assert overridden.external_agent_enabled is True


def test_external_agent_mode_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        EventLensSettings.load(
            Path("/definitely/missing.env"),
            environment={"EVENTLENS_EXTERNAL_AGENT_ENABLED": "sometimes"},
        )


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_external_agent_boolean_spellings_select_external(value: str) -> None:
    settings = EventLensSettings.load(
        Path("/definitely/missing.env"),
        environment={"EVENTLENS_EXTERNAL_AGENT_ENABLED": value},
    )
    assert settings.agent_route() is AgentRoute.EXTERNAL
    assert settings.agent_route_readiness().ready is True


def test_flag_off_selects_deepseek_and_requires_its_key() -> None:
    without_key = EventLensSettings.load(
        Path("/definitely/missing.env"),
        environment={
            "EVENTLENS_EXTERNAL_AGENT_ENABLED": "false",
            "EVENTLENS_AGENT_PROVIDER": "fixture",
        },
    )
    assert without_key.agent_route() is AgentRoute.DEEPSEEK
    assert without_key.agent_route_readiness().ready is False
    with_key = without_key.model_copy(update={"deepseek_api_key": "not-rendered"})
    assert with_key.agent_route_readiness().ready is True


def test_route_readiness_is_secret_safe() -> None:
    settings = EventLensSettings.model_validate(
        {"external_agent_enabled": False, "deepseek_api_key": "super-secret"}
    )
    readiness = settings.agent_route_readiness()
    assert "super-secret" not in str(readiness)
