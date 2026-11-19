"""Non-blocking application jobs for the Streamlit boundary.

Streamlit reruns a script from top to bottom for every interaction.  Running
an async workflow directly in that script makes the browser wait for provider
calls and, more importantly, makes a double click capable of submitting the
same durable operation twice.  This module is deliberately UI-neutral: it
owns a small thread pool, runs an async operation in its worker thread, and
exposes immutable snapshots for a renderer to poll.

The coordinator is not a second workflow engine.  It does not retry, mutate
workflow state, or infer progress.  A caller supplies a stable key for the
operation and is responsible for rendering the returned result.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, Generic, TypeVar


T = TypeVar("T")
AsyncOperation = Callable[[], Awaitable[T] | T]


class JobStatus(StrEnum):
    """Lifecycle states visible to a UI or other polling client."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
_TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass(frozen=True)
class JobSnapshot(Generic[T]):
    """Immutable polling result.

    ``duplicate`` is true only on the snapshot returned by a submission that
    found an already-active operation with the same key.  It is intentionally
    not part of the lifecycle status so callers can render a warning while
    continuing to poll the original job.
    """

    job_id: str
    key: str
    status: JobStatus
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: float | None = None
    message: str | None = None
    result: T | None = None
    error: str | None = None
    error_type: str | None = None
    duplicate: bool = False

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


@dataclass
class _JobRecord:
    snapshot: JobSnapshot[Any]
    operation: AsyncOperation[Any]


class UnknownJobError(KeyError):
    """Raised when a caller polls or updates a job that was never submitted."""


class BackgroundJobCoordinator:
    """Thread-safe coordinator for async application calls.

    The lock protects snapshots and the active-key index.  The application
    call itself never runs under that lock.  ``asyncio.run`` is confined to a
    worker thread, keeping Streamlit's script thread free to render and
    rerun.  A coordinator can be kept in a Streamlit session state or reused
    by a test; it has no Streamlit dependency.
    """

    def __init__(self, *, max_workers: int = 2, thread_name_prefix: str = "eventlens-job"):
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._jobs: dict[str, _JobRecord] = {}
        self._active_by_key: dict[str, str] = {}
        self._lock = threading.RLock()
        self._closed = False

    def submit(self, key: str, operation: AsyncOperation[T]) -> JobSnapshot[T]:
        """Queue one operation, reusing an active job for duplicate keys.

        ``operation`` is a factory rather than a coroutine object so a
        duplicate submit does not create an unawaited coroutine. A terminal
        job keeps reserving its key until the UI explicitly consumes
        and removes it. This closes the completion-before-rerender race that
        would otherwise dispatch the same provider operation twice.
        """

        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("job key must not be empty")
        if not callable(operation):
            raise TypeError("operation must be callable")
        submitted_at = _utcnow()
        with self._lock:
            self._ensure_open()
            existing_id = self._active_by_key.get(normalized_key)
            if existing_id is not None:
                existing = self._jobs[existing_id].snapshot
                return replace(existing, duplicate=True)
            job_id = uuid.uuid4().hex
            snapshot = JobSnapshot[T](
                job_id=job_id,
                key=normalized_key,
                status=JobStatus.QUEUED,
                submitted_at=submitted_at,
                progress=0.0,
                message="Queued",
            )
            record = _JobRecord(snapshot=snapshot, operation=operation)
            self._jobs[job_id] = record
            self._active_by_key[normalized_key] = job_id
            try:
                self._executor.submit(self._execute, job_id)
            except BaseException:
                self._jobs.pop(job_id, None)
                self._active_by_key.pop(normalized_key, None)
                raise
            return snapshot

    def poll(self, job_id: str) -> JobSnapshot[Any]:
        """Return the latest immutable state for ``job_id``."""

        with self._lock:
            return self._record(job_id).snapshot

    def poll_key(self, key: str) -> JobSnapshot[Any] | None:
        """Return the active job for ``key``, if one exists."""

        with self._lock:
            job_id = self._active_by_key.get(str(key).strip())
            if job_id is None:
                return None
            return self._record(job_id).snapshot

    def update_progress(
        self, job_id: str, progress: float | None, *, message: str | None = None
    ) -> JobSnapshot[Any]:
        """Update optional caller-owned progress metadata for a queued job."""

        if progress is not None and not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        with self._lock:
            record = self._record(job_id)
            if record.snapshot.terminal:
                return record.snapshot
            record.snapshot = replace(record.snapshot, progress=progress, message=message)
            return record.snapshot

    def cleanup(
        self, *, max_age: timedelta = timedelta(minutes=30), now: datetime | None = None
    ) -> tuple[str, ...]:
        """Remove old terminal snapshots and return their IDs.

        Cleanup never removes queued/running jobs, which makes it safe to call
        at the beginning of every Streamlit rerun.
        """

        if max_age.total_seconds() < 0:
            raise ValueError("max_age must not be negative")
        current = now or _utcnow()
        removed: list[str] = []
        with self._lock:
            for job_id, record in list(self._jobs.items()):
                snapshot = record.snapshot
                if not snapshot.terminal or snapshot.finished_at is None:
                    continue
                if current - snapshot.finished_at < max_age:
                    continue
                self._jobs.pop(job_id, None)
                self._active_by_key.pop(snapshot.key, None)
                removed.append(job_id)
        return tuple(removed)

    def remove(self, job_id: str) -> JobSnapshot[Any]:
        """Remove one terminal job after its result has been consumed."""

        with self._lock:
            record = self._record(job_id)
            if not record.snapshot.terminal:
                raise ValueError("cannot remove an active job")
            self._jobs.pop(job_id)
            self._active_by_key.pop(record.snapshot.key, None)
            return record.snapshot

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop accepting work and release worker threads."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _execute(self, job_id: str) -> None:
        with self._lock:
            record = self._record(job_id)
            started_at = _utcnow()
            record.snapshot = replace(
                record.snapshot,
                status=JobStatus.RUNNING,
                started_at=started_at,
                progress=0.05,
                message="Running",
            )
        try:
            value = record.operation()
            if inspect.isawaitable(value):
                value = asyncio.run(value)
        except asyncio.CancelledError:
            self._finish(job_id, status=JobStatus.CANCELLED, message="Cancelled")
        except BaseException as error:  # worker failures must be pollable, not lost
            self._finish(
                job_id,
                status=JobStatus.FAILED,
                error=_safe_error(error),
                error_type=type(error).__name__,
                message="Failed",
            )
        else:
            self._finish(
                job_id,
                status=JobStatus.SUCCEEDED,
                result=value,
                progress=1.0,
                message="Complete",
            )

    def _finish(self, job_id: str, *, status: JobStatus, **changes: Any) -> None:
        with self._lock:
            record = self._record(job_id)
            record.snapshot = replace(
                record.snapshot,
                status=status,
                finished_at=_utcnow(),
                **changes,
            )
            # Keep the key reserved until the terminal result is consumed via
            # remove() (or aged out through cleanup()).

    def _record(self, job_id: str) -> _JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError as error:
            raise UnknownJobError(job_id) from error

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("job coordinator is shut down")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error(error: BaseException) -> str:
    # Provider exception strings frequently contain response bodies, URLs,
    # prompts, or credentials. Detailed diagnostics belong in a separately
    # redacted audit record, never in the browser polling contract.
    return f"{type(error).__name__} while running the background action"


__all__ = [
    "BackgroundJobCoordinator",
    "JobSnapshot",
    "JobStatus",
    "UnknownJobError",
]
