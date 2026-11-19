from datetime import UTC, date, datetime

import pytest

from eventlens.live_preflight import ReleaseTimeBasis, UpcomingLivePreflight


def _valid(**changes):
    payload = {
        "ticker": "DEMO",
        "event_id": "DEMO:2026-10-28:2027Q1",
        "event_date": date(2026, 10, 28),
        "event_release_at": datetime(2026, 10, 28, 20, 0, tzinfo=UTC),
        "release_time_basis": ReleaseTimeBasis.ASSUMED_AFTER_MARKET_CLOSE,
        "timing_warning": "Issuer timestamp is not yet confirmed; assumed AMC.",
        "prior_fiscal_quarter": "2026Q4",
        "post_event_trading_dates": (
            date(2026, 10, 29),
            date(2026, 10, 30),
            date(2026, 11, 2),
            date(2026, 11, 3),
            date(2026, 11, 4),
        ),
    }
    payload.update(changes)
    return UpcomingLivePreflight(**payload)


def test_live_preflight_is_secret_free_and_requires_approval() -> None:
    request = _valid()
    assert request.ticker == "DEMO"
    assert request.approval_required is True
    assert "api_key" not in request.model_dump()


def test_assumed_timing_requires_warning() -> None:
    with pytest.raises(ValueError, match="explicit warning"):
        _valid(timing_warning=None)


def test_preflight_rejects_bad_reaction_window() -> None:
    with pytest.raises(ValueError, match="exactly five"):
        _valid(post_event_trading_dates=(date(2026, 10, 29),))


def test_preflight_cli_is_ticker_generic_with_legacy_alias() -> None:
    pytest.importorskip("exchange_calendars")
    from eventlens.app.commands import build_parser

    parser = build_parser()
    modern = parser.parse_args(
        [
            "upcoming-preflight",
            "--ticker",
            "DEMO",
            "--window-start",
            "2026-10-27",
            "--window-end",
            "2026-10-30",
        ]
    )
    legacy = parser.parse_args(
        [
            "upcoming-demo-preflight",
            "--ticker",
            "DEMO",
            "--window-start",
            "2026-10-27",
            "--window-end",
            "2026-10-30",
        ]
    )
    assert modern.ticker == legacy.ticker == "DEMO"


def test_provider_calls_require_explicit_ticker() -> None:
    from eventlens.app.commands import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "upcoming-preflight",
                "--window-start",
                "2026-10-27",
                "--window-end",
                "2026-10-30",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "provider-smoke",
                "--provider",
                "finnhub",
                "--capability",
                "earnings-events",
            ]
        )


def test_massive_news_smoke_requires_explicit_window_and_accepts_cutoff() -> None:
    from eventlens.app.commands import build_parser

    args = build_parser().parse_args(
        [
            "provider-smoke",
            "--provider",
            "massive",
            "--capability",
            "company-news",
            "--ticker",
            "DEMO",
            "--start",
            "2026-07-11",
            "--end",
            "2026-08-10",
            "--cutoff-at",
            "2026-08-10T19:30:00+00:00",
        ]
    )

    assert args.provider == "massive"
    assert args.cutoff_at == datetime(2026, 8, 10, 19, 30, tzinfo=UTC)
