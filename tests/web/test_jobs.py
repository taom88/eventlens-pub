import asyncio
import threading
import time
from datetime import timedelta

import pytest

from eventlens.web.jobs import (
    BackgroundJobCoordinator,
    JobStatus,
    UnknownJobError,
)


def _wait_for_terminal(coordinator: BackgroundJobCoordinator, job_id: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = coordinator.poll(job_id)
        if snapshot.terminal:
            return snapshot
        time.sleep(0.005)
    raise AssertionError("background job did not reach a terminal state")


def test_async_operation_runs_off_caller_thread_and_can_be_polled() -> None:
    coordinator = BackgroundJobCoordinator(max_workers=1)
    caller_thread = threading.get_ident()
    operation_thread: list[int] = []
    try:
        async def operation() -> str:
            operation_thread.append(threading.get_ident())
            await asyncio.sleep(0.02)
            return "done"

        submitted = coordinator.submit("session:start", operation)
        assert submitted.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        finished = _wait_for_terminal(coordinator, submitted.job_id)
        assert finished.status is JobStatus.SUCCEEDED
        assert finished.result == "done"
        assert operation_thread and operation_thread[0] != caller_thread
        assert finished.progress == 1.0
    finally:
        coordinator.shutdown(wait=True)


def test_duplicate_active_submission_reuses_original_without_creating_coroutine() -> None:
    coordinator = BackgroundJobCoordinator(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.to_thread(release.wait, 1)
        return "one result"

    try:
        first = coordinator.submit("session:duplicate", operation)
        assert started.wait(1)
        duplicate = coordinator.submit("session:duplicate", operation)
        assert duplicate.duplicate is True
        assert duplicate.job_id == first.job_id
        release.set()
        finished = _wait_for_terminal(coordinator, first.job_id)
        assert finished.result == "one result"
        assert calls == 1
    finally:
        release.set()
        coordinator.shutdown(wait=True)


def test_failure_is_a_pollable_safe_terminal_result() -> None:
    coordinator = BackgroundJobCoordinator()
    try:
        async def operation() -> None:
            raise RuntimeError("provider unavailable")

        submitted = coordinator.submit("session:error", operation)
        finished = _wait_for_terminal(coordinator, submitted.job_id)
        assert finished.status is JobStatus.FAILED
        assert finished.error_type == "RuntimeError"
        assert finished.error == "RuntimeError while running the background action"
    finally:
        coordinator.shutdown(wait=True)


def test_terminal_result_reserves_key_until_consumed() -> None:
    coordinator = BackgroundJobCoordinator(max_workers=1)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "done"

    try:
        first = coordinator.submit("session:stage:revision", operation)
        finished = _wait_for_terminal(coordinator, first.job_id)
        duplicate = coordinator.submit("session:stage:revision", operation)
        assert duplicate.duplicate is True
        assert duplicate.job_id == finished.job_id
        assert calls == 1

        coordinator.remove(finished.job_id)
        second = coordinator.submit("session:stage:revision", operation)
        assert second.job_id != first.job_id
        _wait_for_terminal(coordinator, second.job_id)
        assert calls == 2
    finally:
        coordinator.shutdown(wait=True)


def test_cleanup_removes_old_terminal_results_but_not_active_jobs() -> None:
    coordinator = BackgroundJobCoordinator(max_workers=1)
    release = threading.Event()

    async def active_operation() -> str:
        await asyncio.to_thread(release.wait, 1)
        return "active"

    try:
        active = coordinator.submit("session:active", active_operation)
        assert coordinator.cleanup(max_age=timedelta(0)) == ()
        release.set()
        finished = _wait_for_terminal(coordinator, active.job_id)
        removed = coordinator.cleanup(max_age=timedelta(0))
        assert finished.job_id in removed
        with pytest.raises(UnknownJobError):
            coordinator.poll(finished.job_id)
    finally:
        release.set()
        coordinator.shutdown(wait=True)
