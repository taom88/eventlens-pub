import json
from pathlib import Path

import pytest

from eventlens.agents import (
    DeterministicExternalAgentFixture,
    ExternalAgentBundle,
    ExternalReportValidationError,
    parse_external_report_json,
    validate_external_report,
)


CASES = Path(__file__).parents[2] / "fixtures" / "external_agent" / "v1" / "cases.json"


def test_checked_in_fixture_agent_output_is_reproducible_and_valid() -> None:
    package = json.loads(CASES.read_text(encoding="utf-8"))
    bundle = ExternalAgentBundle.model_validate(package["bundle"])
    expected = package["valid_report"]

    report = DeterministicExternalAgentFixture().interpret(bundle)

    assert report.model_dump(mode="json") == expected
    assert validate_external_report(bundle, report).report == report


@pytest.mark.parametrize(
    "case_name",
    (
        "stale_checksum",
        "unknown_citation",
        "unknown_metric",
        "bad_probability",
        "bad_action",
        "unsupported_zone",
        "duplicate_claim_id",
    ),
)
def test_invalid_fixture_reports_fail_for_the_declared_reason(case_name: str) -> None:
    package = json.loads(CASES.read_text(encoding="utf-8"))
    bundle = ExternalAgentBundle.model_validate(package["bundle"])
    case = package["invalid_reports"][case_name]

    with pytest.raises(ExternalReportValidationError) as caught:
        report = parse_external_report_json(json.dumps(case["report"]))
        validate_external_report(bundle, report)

    assert caught.value.code.value == case["expected_code"]
