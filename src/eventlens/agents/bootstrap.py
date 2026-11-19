"""Default agent skill/tool registry and provider adapter bootstrap."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from .reasoning import (
    DeepSeekReasoningAdapter,
    DeterministicFixtureReasoningAdapter,
    MoonshotKimiReasoningAdapter,
    ReasoningAdapter,
)
from .skills import DEFAULT_SKILLS
from .tools import DEFAULT_TOOLS


class ReasoningProviderSettings(BaseModel):
    """Secret-safe settings passed directly to an adapter, never to logs or manifests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    api_key: SecretStr | None = None
    base_url: str | None = None
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    max_tokens: int = 4_000


def bootstrap_reasoning_adapter(settings: ReasoningProviderSettings) -> ReasoningAdapter:
    provider = settings.provider.casefold()
    if provider == "fixture":
        return DeterministicFixtureReasoningAdapter(skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS)
    if settings.api_key is None:
        raise ValueError(f"{settings.provider} reasoning requires an API key")
    if provider == "deepseek":
        return DeepSeekReasoningAdapter(
            api_key=settings.api_key, model=settings.model, base_url=settings.base_url,
            skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS,
            timeout_seconds=settings.timeout_seconds, max_attempts=settings.max_attempts,
            max_tokens=settings.max_tokens,
        )
    if provider in {"moonshot", "kimi", "moonshot_kimi"}:
        return MoonshotKimiReasoningAdapter(
            api_key=settings.api_key, model=settings.model, base_url=settings.base_url,
            skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS,
            timeout_seconds=settings.timeout_seconds, max_attempts=settings.max_attempts,
            max_tokens=settings.max_tokens,
        )
    raise ValueError("supported direct reasoning providers are DeepSeek and Moonshot/Kimi")
