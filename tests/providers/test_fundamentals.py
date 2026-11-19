import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eventlens.providers.fixture import FixtureProvider
from eventlens.providers.models import FundamentalObservation, FundamentalSnapshot


def _write_fixture(tmp_path):
    (tmp_path / "BETA.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "ticker": "BETA",
                "retrieved_at": "2026-09-29T12:00:00Z",
                "fundamentals": [
                    {
                        "metric_key": "revenue",
                        "value_numeric": "100",
                        "unit": "USD_MILLION",
                        "fiscal_period": "FY2026_Q2",
                        "period_end": "2026-02-28",
                        "accounting_basis": "GAAP",
                        "available_at": "2026-03-20T12:00:00Z",
                        "source_ref": "fixture:BETA:revenue",
                    },
                    {
                        "metric_key": "diluted_eps",
                        "value_numeric": None,
                        "unit": "USD_PER_SHARE",
                        "fiscal_period": "FY2026_Q2",
                        "accounting_basis": "GAAP",
                        "available_at": "2026-03-20T12:00:00Z",
                        "source_ref": "fixture:BETA:eps",
                        "missing_reason": "not reported in fixture",
                    },
                    {
                        "metric_key": "future_metric",
                        "value_numeric": "999",
                        "unit": "USD",
                        "fiscal_period": "FY2026_Q2",
                        "accounting_basis": "GAAP",
                        "available_at": "2026-04-01T12:00:00Z",
                        "source_ref": "fixture:BETA:future",
                    },
                ],
            }
        )
    )


def test_fixture_fundamentals_are_cutoff_safe_and_provenance_wrapped(tmp_path) -> None:
    _write_fixture(tmp_path)
    response = FixtureProvider(tmp_path).get_fundamentals(
        "BETA", datetime(2026, 3, 21, tzinfo=timezone.utc), fiscal_period="FY2026_Q2"
    )
    assert response.provenance.request_parameters["as_of_at"].startswith("2026-03-21")
    assert response.provenance.quota_consumed is False
    by_metric = {item.metric_key: item for item in response.data.observations}
    assert by_metric["revenue"].value_numeric == Decimal("100")
    assert by_metric["diluted_eps"].missing_reason == "not reported in fixture"
    assert "future_metric" not in by_metric


def test_fundamental_snapshot_rejects_post_cutoff_observations() -> None:
    with pytest.raises(ValueError, match="after the snapshot cutoff"):
        FundamentalSnapshot(
            ticker="BETA",
            as_of_at=datetime(2026, 3, 20, tzinfo=timezone.utc),
            observations=(
                FundamentalObservation(
                    metric_key="revenue", value_numeric=Decimal("1"), unit="USD",
                    fiscal_period="FY2026_Q2", accounting_basis="GAAP",
                    available_at=datetime(2026, 3, 21, tzinfo=timezone.utc),
                    source_ref="fixture:future",
                ),
            ),
        )
