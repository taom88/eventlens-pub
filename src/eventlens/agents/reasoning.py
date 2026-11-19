"""Fixture and OpenAI-compatible adapters for constrained analyst reasoning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from .models import (
    AgentErrorCode,
    AgentFailure,
    AgentReasoningResult,
    AgentReportDraft,
    AnalystInterpretationRequest,
    AnalystReport,
    ClaimKind,
    Citation,
    EvidenceClaim,
    validate_report_evidence,
)
from .prompts import build_messages
from .models import AgentSkill, AgentTool


@runtime_checkable
class ReasoningAdapter(Protocol):
    async def interpret(self, request: AnalystInterpretationRequest) -> AgentReasoningResult: ...


class DeterministicFixtureReasoningAdapter:
    """No-network adapter that yields a stable evidence-grounded report per request."""

    provider = "fixture"
    model = "deterministic-evidence-v1"

    def __init__(
        self,
        *,
        fixtures: Mapping[str, AgentReportDraft] | None = None,
        skills: tuple[AgentSkill, ...],
        tools: tuple[AgentTool, ...],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fixtures = dict(fixtures or {})
        self._skills = skills
        self._tools = tools
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def interpret(self, request: AnalystInterpretationRequest) -> AgentReasoningResult:
        try:
            build_messages(request, skills=self._skills, tools=self._tools)
            draft = self._fixtures.get(request.request_id) or _draft_from_first_evidence(request)
            validate_report_evidence(draft, request)
            return _success_result(
                request,
                draft,
                provider=self.provider,
                model=self.model,
                generated_at=self._clock(),
                attempts=0,
            )
        except ValueError:
            return _failure_result(
                request,
                provider=self.provider,
                model=self.model,
                attempts=0,
                code=AgentErrorCode.INVALID_EVIDENCE,
                message="Fixture report did not satisfy EventLens evidence or allowlist rules.",
            )


class DurableCachedReasoningAdapter:
    """Persist successful typed reasoning results by complete request digest."""

    def __init__(self, inner: ReasoningAdapter, cache_dir: Path) -> None:
        self._inner = inner
        self._cache_dir = cache_dir.resolve()
        self._locks: dict[str, asyncio.Lock] = {}
        self.provider = getattr(inner, "provider", "reasoning")
        self.model = getattr(inner, "model", "unknown")

    async def interpret(self, request: AnalystInterpretationRequest) -> AgentReasoningResult:
        request_bytes = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(request_bytes).hexdigest()
        lock = self._locks.setdefault(digest, asyncio.Lock())
        async with lock:
            cached = self._read(digest)
            if cached is not None:
                return cached
            result = await self._inner.interpret(request)
            if result.report is not None:
                self._write(digest, result)
            return result

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()

    def _read(self, digest: str) -> AgentReasoningResult | None:
        path = self._path(digest)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("request_sha256") != digest:
                raise ValueError("reasoning cache request digest mismatch")
            return AgentReasoningResult.model_validate(envelope["result"])
        except (OSError, KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            raise RuntimeError("reasoning cache failed integrity validation") from error

    def _write(self, digest: str, result: AgentReasoningResult) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(digest)
        content = (
            json.dumps(
                {"request_sha256": digest, "result": result.model_dump(mode="json")},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".reasoning-", dir=self._cache_dir
        )
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

    def _path(self, digest: str) -> Path:
        path = (self._cache_dir / f"{digest}.json").resolve()
        if path.parent != self._cache_dir:
            raise ValueError("reasoning cache path escaped its configured root")
        return path
class OpenAICompatibleReasoningAdapter:
    """Direct OpenAI-compatible JSON adapter with injectable httpx transport.

    The adapter deliberately returns a secret-safe typed error envelope rather
    than retaining raw provider error bodies or completion text.
    """

    provider = "openai_compatible"
    completion_token_field = "max_tokens"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        base_url: str,
        skills: tuple[AgentSkill, ...],
        tools: tuple[AgentTool, ...],
        http: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        max_tokens: int = 4_000,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if http is not None and transport is not None:
            raise ValueError("provide either an http client or a transport, not both")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._api_key = api_key
        self.model = model
        self._base_url = _validated_keyed_base_url(base_url)
        self._skills = skills
        self._tools = tools
        self._http = http or httpx.AsyncClient(transport=transport, timeout=timeout_seconds)
        self._owns_http = http is None
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._max_tokens = max_tokens
        self._request_overrides: dict[str, Any] = {}
        self._sleeper = sleeper or asyncio.sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def interpret(self, request: AnalystInterpretationRequest) -> AgentReasoningResult:
        try:
            messages = build_messages(request, skills=self._skills, tools=self._tools)
        except ValueError:
            return _failure_result(
                request,
                provider=self.provider,
                model=self.model,
                attempts=0,
                code=AgentErrorCode.POLICY_VIOLATION,
                message="Agent request includes a tool or skill outside the approved allowlist.",
            )
        request_body = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0,
            self.completion_token_field: self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        request_body.update(self._request_overrides)
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}", "Content-Type": "application/json"}
        repair_used = False
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._http.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=request_body,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException:
                if attempt < self._max_attempts:
                    await self._sleeper(_retry_delay(attempt))
                    continue
                return _failure_result(request, self.provider, self.model, attempt, AgentErrorCode.TIMEOUT, "Reasoning provider timed out.", retryable=True)
            except httpx.TransportError:
                if attempt < self._max_attempts:
                    await self._sleeper(_retry_delay(attempt))
                    continue
                return _failure_result(request, self.provider, self.model, attempt, AgentErrorCode.TRANSPORT, "Reasoning provider transport failed.", retryable=True)
            if response.status_code in {401, 403}:
                return _failure_result(request, self.provider, self.model, attempt, AgentErrorCode.AUTHENTICATION, "Reasoning provider authentication was rejected.")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_attempts:
                    await self._sleeper(_retry_delay(attempt))
                    continue
                return _failure_result(request, self.provider, self.model, attempt, AgentErrorCode.RATE_LIMITED if response.status_code == 429 else AgentErrorCode.UNAVAILABLE, "Reasoning provider is temporarily unavailable.", retryable=True)
            if response.status_code >= 400:
                return _failure_result(request, self.provider, self.model, attempt, AgentErrorCode.UNAVAILABLE, "Reasoning provider rejected the request.")
            completion_payload: Any = None
            try:
                completion_payload = response.json()
                draft = _parse_completion(completion_payload)
                validate_report_evidence(draft, request)
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError, KeyError) as error:
                code, message, category = _reasoning_validation_failure(error)
                completion_metadata = _safe_completion_metadata(completion_payload)
                if not repair_used and attempt < self._max_attempts:
                    request_body["messages"] = list(messages) + [
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "task": "Repair the prior answer and return only the required JSON object.",
                                    "failure_category": category,
                                    "validation_issues": _safe_validation_issues(error),
                                    "completion_metadata": completion_metadata,
                                    "requirements": [
                                        "Use the exact report schema supplied in the original request.",
                                        "Cite only one of the valid source_id/passage_id pairs below.",
                                        "Do not include trading advice or canonical EventLens metrics.",
                                        "Do not wrap the JSON in prose or markdown.",
                                    ],
                                    "valid_citations": [
                                        {"source_id": item.source_id, "passage_id": item.passage_id}
                                        for item in request.evidence
                                    ],
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        }
                    ]
                    repair_used = True
                    continue
                return _failure_result(
                    request,
                    self.provider,
                    self.model,
                    attempt,
                    code,
                    _message_with_safe_validation_issues(
                        message, error, completion_metadata=completion_metadata
                    ),
                )
            return _success_result(request, draft, self.provider, self.model, self._clock(), attempt)
        raise AssertionError("retry loop must return")


def _reasoning_validation_failure(
    error: Exception,
) -> tuple[AgentErrorCode, str, str]:
    if isinstance(error, json.JSONDecodeError):
        return (
            AgentErrorCode.MALFORMED_RESPONSE,
            "Reasoning provider returned invalid JSON after one bounded repair attempt.",
            "invalid_json",
        )
    if isinstance(error, ValidationError):
        return (
            AgentErrorCode.MALFORMED_RESPONSE,
            "Reasoning provider returned JSON that failed the report schema after one bounded repair attempt.",
            "schema_validation",
        )
    if isinstance(error, ValueError):
        return (
            AgentErrorCode.INVALID_EVIDENCE,
            "Reasoning provider returned unsupported advice or invalid evidence citations after one bounded repair attempt.",
            "evidence_or_policy",
        )
    return (
        AgentErrorCode.MALFORMED_RESPONSE,
        "Reasoning provider returned an invalid completion envelope after one bounded repair attempt.",
        "completion_envelope",
    )


def _safe_validation_issues(error: Exception) -> list[dict[str, str]]:
    """Return schema diagnostics without retaining untrusted completion values."""

    if isinstance(error, ValidationError):
        return [
            {
                "location": ".".join(str(item) for item in issue["loc"]),
                "type": str(issue["type"]),
            }
            for issue in error.errors(include_input=False, include_url=False)[:12]
        ]
    if isinstance(error, json.JSONDecodeError):
        return [{"location": "$", "type": "json_invalid"}]
    if isinstance(error, ValueError):
        return [{"location": "$", "type": "evidence_or_policy_validation"}]
    return [{"location": "$", "type": "completion_envelope_invalid"}]


def _message_with_safe_validation_issues(
    message: str,
    error: Exception,
    *,
    completion_metadata: dict[str, object],
) -> str:
    issues = _safe_validation_issues(error)
    details = ", ".join(
        f"{item['location']}:{item['type']}" for item in issues
    )
    metadata = json.dumps(completion_metadata, sort_keys=True, separators=(",", ":"))
    return f"{message} Safe validation issues: {details}. Completion metadata: {metadata}."


def _safe_completion_metadata(payload: Any) -> dict[str, object]:
    metadata: dict[str, object] = {
        "content_characters": 0,
        "finish_reason": "unavailable",
        "starts_code_fence": False,
        "starts_json_object": False,
    }
    if not isinstance(payload, dict):
        return metadata
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return metadata
    choice = choices[0]
    metadata["finish_reason"] = str(choice.get("finish_reason") or "unavailable")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return metadata
    normalized = content.lstrip()
    metadata.update({
        "content_characters": len(content),
        "starts_code_fence": normalized.startswith("```"),
        "starts_json_object": normalized.startswith("{"),
    })
    return metadata


class DeepSeekReasoningAdapter(OpenAICompatibleReasoningAdapter):
    provider = "deepseek"

    def __init__(self, *, api_key: SecretStr, model: str, base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url or "https://api.deepseek.com/v1", **kwargs)
        if model.startswith("deepseek-v4"):
            self._request_overrides = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            }


class MoonshotKimiReasoningAdapter(OpenAICompatibleReasoningAdapter):
    provider = "moonshot_kimi"
    completion_token_field = "max_completion_tokens"

    def __init__(self, *, api_key: SecretStr, model: str, base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url or "https://api.moonshot.ai/v1", **kwargs)


def _draft_from_first_evidence(request: AnalystInterpretationRequest) -> AgentReportDraft:
    evidence = request.evidence[0]
    text = evidence.text.strip()
    summary = text[:600]
    return AgentReportDraft(
        headline=f"Evidence-grounded {request.kind.value.replace('_', ' ')} for {request.ticker}",
        executive_summary=summary,
        executive_summary_citations=(
            Citation(source_id=evidence.source_id, passage_id=evidence.passage_id),
        ),
        claims=(
            EvidenceClaim(
                kind=ClaimKind.FACT,
                text=summary,
                citations=(Citation(source_id=evidence.source_id, passage_id=evidence.passage_id),),
            ),
        ),
        caveats=("This fixture interpretation uses only the supplied evidence passages.",),
    )


def _parse_completion(payload: Any) -> AgentReportDraft:
    if not isinstance(payload, dict):
        raise TypeError("completion envelope must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("completion omitted choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ValueError("completion omitted JSON message content")
    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
        normalized = normalized.rsplit("```", 1)[0].strip()
    return AgentReportDraft.model_validate_json(normalized)


def _success_result(request: AnalystInterpretationRequest, draft: AgentReportDraft, provider: str, model: str, generated_at: datetime, attempts: int) -> AgentReasoningResult:
    report = AnalystReport(
        **draft.model_dump(),
        report_id=f"{request.request_id}:report",
        request_id=request.request_id,
        kind=request.kind,
        provider=provider,
        model=model,
        prompt_version=request.prompt_version,
        generated_at=generated_at,
    )
    return AgentReasoningResult(request_id=request.request_id, provider=provider, model=model, attempts=attempts, report=report)


def _failure_result(request: AnalystInterpretationRequest, provider: str, model: str, attempts: int, code: AgentErrorCode, message: str, *, retryable: bool = False) -> AgentReasoningResult:
    return AgentReasoningResult(request_id=request.request_id, provider=provider, model=model, attempts=attempts, errors=(AgentFailure(code=code, message=message, retryable=retryable),))


def _retry_delay(attempt: int) -> float:
    return min(0.25 * (2 ** (attempt - 1)), 2.0)


def _validated_keyed_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("reasoning base URL cannot contain credentials, query, or fragment")
    is_loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if parsed.scheme != "https" and not is_loopback_http:
        raise ValueError("keyed reasoning base URL must use HTTPS or loopback HTTP")
    if not parsed.netloc:
        raise ValueError("reasoning base URL requires a host")
    return value.rstrip("/")
