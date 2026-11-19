"""Fail-closed validation for imported external-agent reports."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .external import ExternalAgentBundle, ExternalAgentReport


class ExternalReportIssueCode(StrEnum):
    MALFORMED_JSON = "malformed_json"
    DUPLICATE_JSON_KEY = "duplicate_json_key"
    SCHEMA_INVALID = "schema_invalid"
    BINDING_MISMATCH = "binding_mismatch"
    UNKNOWN_CITATION = "unknown_citation"
    UNKNOWN_METRIC = "unknown_metric"
    INVALID_EVIDENCE_SUPPORT = "invalid_evidence_support"


class ExternalReportValidationError(ValueError):
    def __init__(self, code: ExternalReportIssueCode, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


class ValidatedExternalReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report: ExternalAgentReport
    cited_evidence_ids: tuple[str, ...]
    referenced_metric_ids: tuple[str, ...]
    validation_version: str = Field(default="external-report-import-v1")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalReportValidationError(
                ExternalReportIssueCode.DUPLICATE_JSON_KEY,
                f"duplicate JSON key: {key}",
            )
        result[key] = value
    return result


def parse_external_report_json(raw: str | bytes) -> ExternalAgentReport:
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_pairs_without_duplicates,
            parse_float=str,
        )
    except ExternalReportValidationError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise ExternalReportValidationError(
            ExternalReportIssueCode.MALFORMED_JSON,
            "external report is not valid JSON",
        ) from error
    try:
        return ExternalAgentReport.model_validate(payload)
    except ValidationError as error:
        location = ".".join(str(item) for item in error.errors()[0].get("loc", ()))
        raise ExternalReportValidationError(
            ExternalReportIssueCode.SCHEMA_INVALID,
            f"external report failed schema validation at {location or 'root'}",
        ) from error


def evidence_id(source_id: str, passage_id: str) -> str:
    return f"{source_id}#{passage_id}"


def validate_external_report(
    bundle: ExternalAgentBundle,
    report: ExternalAgentReport,
) -> ValidatedExternalReport:
    expected_binding = (
        bundle.bundle_id,
        bundle.bundle_revision,
        bundle.content_sha256,
        bundle.session_id,
        bundle.event_id,
    )
    actual_binding = (
        report.bundle_id,
        report.bundle_revision,
        report.bundle_sha256,
        report.session_id,
        report.event_id,
    )
    if actual_binding != expected_binding:
        raise ExternalReportValidationError(
            ExternalReportIssueCode.BINDING_MISMATCH,
            "external report does not bind to the paused bundle revision",
        )

    allowed_citations = {
        (item.source_id, item.passage_id): evidence_id(item.source_id, item.passage_id)
        for item in bundle.evidence
    }
    cited: set[str] = set()
    citations = [citation for claim in report.claims for citation in claim.citations]
    citations.extend(
        citation for scenario in report.scenarios for citation in scenario.citations
    )
    citations.extend(
        citation for zone in report.valuation_zones for citation in zone.citations
    )
    for citation in citations:
        key = (citation.source_id, citation.passage_id)
        if key not in allowed_citations:
            raise ExternalReportValidationError(
                ExternalReportIssueCode.UNKNOWN_CITATION,
                "external report cites evidence outside the exported allowlist",
            )
        cited.add(allowed_citations[key])

    allowed_metrics = {item.metric_id for item in bundle.canonical_metrics}
    referenced_metrics = {
        metric_id
        for scenario in report.scenarios
        for metric_id in scenario.canonical_metric_ids
    }
    referenced_metrics.update(
        metric_id for zone in report.valuation_zones for metric_id in zone.metric_refs
    )
    unknown_metrics = referenced_metrics - allowed_metrics
    if unknown_metrics:
        raise ExternalReportValidationError(
            ExternalReportIssueCode.UNKNOWN_METRIC,
            "external report references a canonical metric outside the bundle",
        )

    if report.recommendation is not None:
        allowed_support = set(allowed_citations.values())
        supporting = set(report.recommendation.supporting_evidence_ids)
        contradicting = set(report.recommendation.contradicting_evidence_ids)
        if not supporting or not supporting <= allowed_support or not contradicting <= allowed_support:
            raise ExternalReportValidationError(
                ExternalReportIssueCode.INVALID_EVIDENCE_SUPPORT,
                "recommendation evidence IDs must resolve to exported passages",
            )
        if supporting & contradicting:
            raise ExternalReportValidationError(
                ExternalReportIssueCode.INVALID_EVIDENCE_SUPPORT,
                "supporting and contradicting evidence IDs must be disjoint",
            )

    return ValidatedExternalReport(
        report=report,
        cited_evidence_ids=tuple(sorted(cited)),
        referenced_metric_ids=tuple(sorted(referenced_metrics)),
    )


__all__ = [
    "ExternalReportIssueCode",
    "ExternalReportValidationError",
    "ValidatedExternalReport",
    "evidence_id",
    "parse_external_report_json",
    "validate_external_report",
]
