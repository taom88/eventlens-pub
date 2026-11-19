"""DeepSeek implementation of the same typed external-agent contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from pathlib import Path

import httpx
from pydantic import SecretStr
from eventlens.config import AgentRoute, EventLensSettings

from .external import (
    ExternalAgentBundle,
    ExternalAgentReport,
    UsageReceipt,
    external_agent_report_json_schema,
)
from .external_handoff import ExternalAgentHandoffStore, ExternalHandoffState
from .external_validation import (
    ExternalReportValidationError,
    ExternalReportIssueCode,
    parse_external_report_json,
    validate_external_report,
)


class ExternalReasoningErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    UNAVAILABLE = "unavailable"
    MALFORMED_RESPONSE = "malformed_response"


class ExternalReasoningError(RuntimeError):
    def __init__(self, code: ExternalReasoningErrorCode, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


class DeepSeekExternalAgentAdapter:
    """No-fallback OpenAI-compatible adapter with one bounded repair attempt."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        base_url: str,
        timeout_seconds: float = 60,
        max_tokens: int = 4_000,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("DeepSeek API key is required")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DeepSeek base URL must be credential-free HTTPS")
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self._http = http or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def interpret(self, bundle: ExternalAgentBundle) -> ExternalAgentReport:
        started_at = datetime.now(timezone.utc)
        base_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "Return one JSON object matching the supplied ExternalAgentReport schema. "
                    "Use only bundle evidence and canonical metric references. Do not add keys, "
                    "change bundle facts, or provide execution instructions."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "bundle": bundle.model_dump(mode="json"),
                        "report_schema": external_agent_report_json_schema(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        messages = list(base_messages)
        last_validation_error: ExternalReportValidationError | None = None
        for attempt in (1, 2):
            response = await self._post(messages)
            content, usage = _completion_content(response)
            try:
                payload = json.loads(content, object_pairs_hook=_reject_duplicate_pairs)
                completed_at = datetime.now(timezone.utc)
                payload["usage"] = UsageReceipt(
                    provider="deepseek",
                    model=self.model,
                    prompt_version="external-agent-v1",
                    started_at=started_at,
                    completed_at=completed_at,
                    input_tokens=_optional_int(usage.get("prompt_tokens")),
                    output_tokens=_optional_int(usage.get("completion_tokens")),
                ).model_dump(mode="json")
                report = parse_external_report_json(json.dumps(payload))
                validate_external_report(bundle, report)
                return report
            except (json.JSONDecodeError, TypeError, KeyError, ExternalReportValidationError) as error:
                last_validation_error = (
                    error
                    if isinstance(error, ExternalReportValidationError)
                    else ExternalReportValidationError(
                        code=ExternalReportIssueCode.MALFORMED_JSON,
                        message="DeepSeek returned malformed JSON",
                    )
                )
                if attempt == 1:
                    messages = base_messages + [
                        {
                            "role": "user",
                            "content": (
                                "The prior JSON failed deterministic validation. Return a complete "
                                "replacement JSON object matching the original schema and bundle. "
                                f"Safe validation reason: {last_validation_error.safe_message}"
                            ),
                        }
                    ]
                    continue
        raise ExternalReasoningError(
            ExternalReasoningErrorCode.MALFORMED_RESPONSE,
            "DeepSeek response failed the external-agent contract after one repair attempt",
        ) from last_validation_error

    async def _post(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": self.max_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ExternalReasoningError(
                ExternalReasoningErrorCode.TIMEOUT, "DeepSeek request timed out"
            ) from error
        except httpx.TransportError as error:
            raise ExternalReasoningError(
                ExternalReasoningErrorCode.TRANSPORT, "DeepSeek transport failed"
            ) from error
        if response.status_code in {401, 403}:
            raise ExternalReasoningError(
                ExternalReasoningErrorCode.AUTHENTICATION,
                "DeepSeek authentication was rejected",
            )
        if response.status_code == 429:
            raise ExternalReasoningError(
                ExternalReasoningErrorCode.RATE_LIMITED, "DeepSeek rate limit was reached"
            )
        if response.status_code >= 400:
            raise ExternalReasoningError(
                ExternalReasoningErrorCode.UNAVAILABLE, "DeepSeek request failed"
            )
        try:
            return response.json()
        except json.JSONDecodeError as error:
            raise ExternalReasoningError(
                ExternalReasoningErrorCode.MALFORMED_RESPONSE,
                "DeepSeek response envelope was not JSON",
            ) from error


def _completion_content(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ExternalReasoningError(
            ExternalReasoningErrorCode.MALFORMED_RESPONSE,
            "DeepSeek response envelope omitted message content",
        ) from error
    if not isinstance(content, str):
        raise ExternalReasoningError(
            ExternalReasoningErrorCode.MALFORMED_RESPONSE,
            "DeepSeek message content was not text",
        )
    usage = payload.get("usage")
    return content, usage if isinstance(usage, dict) else {}


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalReportValidationError(
                code=ExternalReportIssueCode.DUPLICATE_JSON_KEY,
                message=f"duplicate JSON key: {key}",
            )
        result[key] = value
    return result


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


async def run_configured_agent_route(
    bundle: ExternalAgentBundle,
    *,
    settings: EventLensSettings,
    artifact_root: Path,
    deepseek: DeepSeekExternalAgentAdapter | None = None,
) -> ExternalHandoffState:
    """Execute exactly the configured route; never fall back across modes."""

    route = settings.agent_route()
    if bundle.route is not route:
        raise ValueError("bundle route does not match configured agent route")
    store = ExternalAgentHandoffStore(artifact_root)
    if route is AgentRoute.EXTERNAL:
        return store.export(bundle)
    readiness = settings.agent_route_readiness()
    if not readiness.ready or settings.deepseek_api_key is None:
        raise ExternalReasoningError(
            ExternalReasoningErrorCode.AUTHENTICATION,
            "DeepSeek route is not configured",
        )
    created = deepseek is None
    adapter = deepseek or DeepSeekExternalAgentAdapter(
        api_key=settings.deepseek_api_key,
        model=settings.agent_model,
        base_url=settings.deepseek_base_url,
        timeout_seconds=settings.agent_timeout_seconds,
        max_tokens=settings.agent_max_tokens,
    )
    try:
        report = await adapter.interpret(bundle)
        return store.record_deepseek_result(bundle, report)
    finally:
        if created:
            await adapter.aclose()


__all__ = [
    "DeepSeekExternalAgentAdapter",
    "ExternalReasoningError",
    "ExternalReasoningErrorCode",
    "run_configured_agent_route",
]
