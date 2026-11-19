"""Secret-safe HTTP cache and conservative daily quota budgets."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import ProviderOrigin


class ProviderHTTPError(RuntimeError):
    pass


def _safe_response_detail(response: httpx.Response) -> str:
    """Return bounded diagnostics without echoing query credentials."""

    raw = response.content
    digest = hashlib.sha256(raw).hexdigest()
    preview = response.text.replace("\n", " ").replace("\r", " ")[:180]
    preview = re.sub(
        r"(?i)(?:token|apikey|api_key|key)=[^ &;,\"'<>]+",
        lambda match: match.group(0).split("=", 1)[0] + "=<redacted>",
        preview,
    )
    return (
        f"status={response.status_code} content_type={response.headers.get('content-type', 'unknown')} "
        f"body_sha256={digest} body_preview={preview!r}"
    )


class ProviderQuotaExceededError(RuntimeError):
    pass


class HttpFetch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    payload: Any
    raw_bytes: bytes
    origin: ProviderOrigin
    retrieved_at: datetime
    safe_request_parameters: dict[str, str]
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    quota_consumed: bool


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.Lock] = {}


class DailyQuotaLedger:
    """Internal budget guard; it is deliberately lower than provider limits."""

    def __init__(self, path: Path, *, today: Callable[[], date] | None = None) -> None:
        self.path = path
        self._today = today or (lambda: datetime.now(timezone.utc).date())
        with _LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(path.resolve(), threading.Lock())

    def reserve(self, provider: str, daily_budget: int) -> None:
        with self._lock, _file_lock(self.path):
            document = self._read()
            today = self._today().isoformat()
            provider_usage = document.setdefault(today, {}).setdefault(provider, 0)
            if provider_usage >= daily_budget:
                raise ProviderQuotaExceededError(
                    f"internal daily budget exhausted for {provider}; cached/no-key sources remain available"
                )
            document[today][provider] = provider_usage + 1
            _atomic_json_write(self.path, document)

    def usage(self, provider: str) -> int:
        with self._lock, _file_lock(self.path):
            return int(self._read().get(self._today().isoformat(), {}).get(provider, 0))

    def _read(self) -> dict[str, dict[str, int]]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise TypeError
            return document
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderQuotaExceededError(
                "quota ledger is unreadable; refusing live quota spend until it is inspected"
            ) from error


class MinimumIntervalLimiter:
    """Cross-process spacing for providers with published fair-access limits."""

    def __init__(
        self,
        path: Path,
        *,
        epoch: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.path = path
        self._epoch = epoch or time.time
        self._sleep = sleeper or time.sleep
        with _LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(path.resolve(), threading.Lock())

    def wait(self, provider: str, minimum_interval_seconds: float) -> None:
        if minimum_interval_seconds <= 0:
            raise ValueError("minimum_interval_seconds must be positive")
        with self._lock, _file_lock(self.path):
            document = self._read()
            now = self._epoch()
            earliest = float(document.get(provider, 0.0)) + minimum_interval_seconds
            delay = max(0.0, earliest - now)
            if delay:
                self._sleep(delay)
            document[provider] = max(self._epoch(), earliest)
            _atomic_json_write(self.path, document)

    def _read(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(key): float(value) for key, value in document.items()}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raise ProviderQuotaExceededError(
                "rate-limit ledger is unreadable; refusing live request until it is inspected"
            )


class CachedHttpClient:
    def __init__(
        self,
        cache_root: Path,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        rate_limit_epoch: Callable[[], float] | None = None,
        rate_limit_sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.client = client or httpx.Client(timeout=30.0, follow_redirects=False)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.ledger = DailyQuotaLedger(cache_root / "quota-ledger.json")
        self.rate_limiter = MinimumIntervalLimiter(
            cache_root / "rate-limit-ledger.json",
            epoch=rate_limit_epoch,
            sleeper=rate_limit_sleeper,
        )

    def get_json(
        self,
        *,
        provider: str,
        url: str,
        parameters: Mapping[str, str],
        secret_parameter_names: frozenset[str] = frozenset(),
        daily_budget: int | None = None,
        max_age_seconds: int | None = None,
        headers: Mapping[str, str] | None = None,
        payload_validator: Callable[[Any], None] | None = None,
        minimum_interval_seconds: float | None = None,
        live_request_allowed: bool = True,
    ) -> HttpFetch:
        safe_parameters = {
            key: value
            for key, value in parameters.items()
            if key not in secret_parameter_names
        }
        fingerprint = _request_fingerprint(provider, url, safe_parameters)
        cache_path = self.cache_root / provider / f"{fingerprint}.json"
        with _LOCKS_GUARD:
            lock = _PATH_LOCKS.setdefault(cache_path.resolve(), threading.Lock())
        with lock, _file_lock(cache_path):
            cached = self._read_cache(cache_path, max_age_seconds)
            if cached is not None:
                try:
                    cached_raw = base64.b64decode(cached["raw_base64"], validate=True)
                    cached_payload = json.loads(cached_raw)
                    if payload_validator is not None:
                        payload_validator(cached_payload)
                except Exception:
                    cache_path.unlink(missing_ok=True)
                else:
                    return HttpFetch(
                        payload=cached_payload,
                        raw_bytes=cached_raw,
                        origin=ProviderOrigin.CACHE,
                        retrieved_at=datetime.fromisoformat(cached["retrieved_at"]),
                        safe_request_parameters=safe_parameters,
                        request_fingerprint=fingerprint,
                        quota_consumed=False,
                    )
            if not live_request_allowed:
                raise ProviderHTTPError(
                    f"{provider} cache-only request missed; live request is not allowed"
                )
            if daily_budget is not None:
                self.ledger.reserve(provider, daily_budget)
            if minimum_interval_seconds is not None:
                self.rate_limiter.wait(provider, minimum_interval_seconds)
            try:
                response = self.client.get(url, params=parameters, headers=headers)
                response.raise_for_status()
                payload = response.json()
                if payload_validator is not None:
                    payload_validator(payload)
            except httpx.HTTPStatusError as error:
                detail = _safe_response_detail(error.response)
                raise ProviderHTTPError(f"{provider} request failed: {detail}") from error
            except httpx.RequestError as error:
                raise ProviderHTTPError(
                    f"{provider} request failed at transport layer: {type(error).__name__}"
                ) from error
            except json.JSONDecodeError as error:
                detail = _safe_response_detail(response)
                raise ProviderHTTPError(f"{provider} response was not usable JSON: {detail}") from error
            retrieved_at = self._clock().astimezone(timezone.utc)
            raw = response.content
            _atomic_json_write(
                cache_path,
                {
                    "retrieved_at": retrieved_at.isoformat(),
                    "raw_base64": base64.b64encode(raw).decode("ascii"),
                },
            )
            return HttpFetch(
                payload=payload,
                raw_bytes=raw,
                origin=ProviderOrigin.LIVE,
                retrieved_at=retrieved_at,
                safe_request_parameters=safe_parameters,
                request_fingerprint=fingerprint,
                quota_consumed=daily_budget is not None,
            )

    def _read_cache(
        self, path: Path, max_age_seconds: int | None
    ) -> dict[str, str] | None:
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            retrieved_at = datetime.fromisoformat(document["retrieved_at"])
            document["raw_base64"]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            path.unlink(missing_ok=True)
            return None
        if max_age_seconds is None:
            return document
        age = (self._clock().astimezone(timezone.utc) - retrieved_at).total_seconds()
        return document if age <= max_age_seconds else None


def raw_sha256(fetch: HttpFetch) -> str:
    return hashlib.sha256(fetch.raw_bytes).hexdigest()


def _request_fingerprint(provider: str, url: str, parameters: Mapping[str, str]) -> str:
    canonical = json.dumps(
        {"provider": provider, "url": url, "parameters": dict(parameters)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_json_write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _file_lock(target: Path):
    """Serialize quota/cache writes across local EventLens processes on POSIX."""

    lock_path = target.with_name(f".{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows falls back to in-process locks.
            yield
            return
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
