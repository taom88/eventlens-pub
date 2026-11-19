"""Key-free deterministic fixture replay package construction."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from eventlens.providers.fixture import FixtureProvider
from eventlens.quant.backtest import run_event_study

from .models import ArtifactEnvelope, ArtifactSourceRef
from .store import AtomicArtifactStore


class FixtureReplayError(ValueError):
    """Raised when a requested fixture event cannot be replayed safely."""


def build_fixture_replay(
    *,
    ticker: str,
    event_id: str,
    fixture_root: Path,
    session_id: str | None = None,
) -> ArtifactEnvelope:
    """Build one canonical event-study artifact without external providers."""

    provider = FixtureProvider(fixture_root)
    events_response = provider.get_earnings_events(ticker, date(1900, 1, 1), date(2100, 1, 1))
    matches = tuple(item for item in events_response.data.events if item.source_event_id == event_id)
    if len(matches) != 1:
        raise FixtureReplayError(f"fixture event {event_id!r} must resolve to exactly one event")
    event = matches[0]
    history_response = provider.get_daily_history(ticker, date(1900, 1, 1), date(2100, 1, 1))
    report = run_event_study(ticker, (event,), history_response.data)
    effective_session = session_id or f"replay:{ticker.upper()}:{event_id}"
    payload = {
        "fixture_notice": "Synthetic demonstration data only; not represented as market history.",
        "report": report.model_dump(mode="json"),
        "provider_warnings": [
            {"code": warning.code, "message": warning.message}
            for warning in (*events_response.provenance.warnings, *history_response.provenance.warnings)
        ],
    }
    source_sha = events_response.provenance.raw_response_sha256
    return ArtifactEnvelope.from_payload(
        artifact_type="fixture_event_replay",
        artifact_id=f"{ticker.upper()}:{event_id}:replay",
        session_id=effective_session,
        event_id=event_id,
        source_lineage=(
            ArtifactSourceRef(
                source_type="fixture",
                source_id=f"{ticker.upper()}.json",
                source_sha256=source_sha,
            ),
        ),
        input_sha256=report.input_sha256,
        calculation_version=report.calculation_version,
        payload=payload,
        created_at=events_response.provenance.retrieved_at,
    )


def write_fixture_replay(
    *,
    ticker: str,
    event_id: str,
    fixture_root: Path,
    output: Path,
    session_id: str | None = None,
) -> tuple[ArtifactEnvelope, str]:
    envelope = build_fixture_replay(
        ticker=ticker, event_id=event_id, fixture_root=fixture_root, session_id=session_id
    )
    digest = AtomicArtifactStore().write(envelope, output)
    return envelope, digest


__all__ = ["FixtureReplayError", "build_fixture_replay", "write_fixture_replay"]
