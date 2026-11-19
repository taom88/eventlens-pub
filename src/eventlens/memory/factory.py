"""Build the configured durable repository without exposing secret values."""

from __future__ import annotations

from eventlens.config.settings import EventLensSettings

from .protocols import MemoryRepositoryError, ResearchMemoryRepository
from .sqlite import SQLiteResearchMemoryRepository
from .supabase import SupabaseResearchMemoryRepository


def repository_from_settings(settings: EventLensSettings) -> ResearchMemoryRepository:
    if settings.memory_backend == "local":
        return SQLiteResearchMemoryRepository(settings.memory_sqlite_path)
    if not settings.supabase_url or settings.supabase_secret_key is None:
        raise MemoryRepositoryError("Supabase memory backend is not configured")
    return SupabaseResearchMemoryRepository(
        url=settings.supabase_url,
        secret_key=settings.supabase_secret_key,
        timeout_seconds=settings.supabase_request_timeout_seconds,
    )
