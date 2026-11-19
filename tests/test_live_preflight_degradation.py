from datetime import date

from eventlens.live_preflight import _prior_quarter


def test_prior_quarter_is_deterministic_for_q3() -> None:
    assert _prior_quarter("2026Q3") == "2026Q2"


def test_prior_quarter_unknown_input_is_explicit() -> None:
    assert _prior_quarter(None) == "unknown"
