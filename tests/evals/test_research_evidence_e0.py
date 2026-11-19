from __future__ import annotations

import hashlib
import json
import socket
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from eventlens.agents.news import NewsClassificationResult
from eventlens.artifacts.models import canonical_json_bytes, sha256_hex
from eventlens.config import AgentRoute
from eventlens.context_cards import ContextCardV4
from eventlens.product.news_signal import aggregate_news_signal
from eventlens.product.research_evidence import (
    EvidenceReceipt,
    NewlyCoveredIssuerEligibility,
    PriorContextCardEvidence,
    ResearchEvidenceBundle,
    ResearchEvidenceStore,
    assemble_research_evidence_bundle,
)
from eventlens.product.synthesis_input import external_bundle_from_research_evidence
from eventlens.providers.content import ContentRetentionPolicy
from tests.agents.test_news import CUTOFF as NEWS_CUTOFF
from tests.agents.test_news import _request, _result
from tests.context_cards.test_schema import _minimal_card
from tests.product.test_research_evidence import _bundle, _evidence_for_news_signal
from tests.product.test_synthesis_input import _populated_bundle

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = (
    ROOT
    / "fixtures"
    / "evals"
    / "external_ready"
    / "research_evidence"
    / "e0-suite-v1.json"
)
SUITE_SHA_PATH = SUITE_PATH.with_suffix(".json.sha256")
RESULT_PATH = ROOT / "docs" / "evaluations" / "e0-mvp401-research-evidence.json"
RESULT_MD_PATH = RESULT_PATH.with_suffix(".md")
EVALUATOR_SOURCE_PATHS = (
    ROOT / "tests" / "agents" / "test_news.py",
    ROOT / "tests" / "context_cards" / "test_schema.py",
    ROOT / "tests" / "evals" / "test_research_evidence_e0.py",
    ROOT / "tests" / "product" / "test_research_evidence.py",
    ROOT / "tests" / "product" / "test_synthesis_input.py",
)


class _EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class E0Case(_EvalModel):
    case_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    critical: bool
    expected_status: Literal["pass", "blocked"]
    expected_reason_codes: tuple[str, ...] = Field(min_length=1)
    required_evidence_kinds: tuple[str, ...] = ()
    required_metric_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def labels_are_canonical(self) -> E0Case:
        for values in (
            self.expected_reason_codes,
            self.required_evidence_kinds,
            self.required_metric_ids,
        ):
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError("E0 case labels must be sorted and unique")
        return self


class E0Suite(_EvalModel):
    schema_version: Literal["eventlens.external-ready-e0-suite.v1"]
    suite_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    production_boundary_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    target_component: Literal["eventlens.research-evidence.v2"]
    base_input: dict[str, str]
    cases: tuple[E0Case, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def cases_are_canonical(self) -> E0Suite:
        ids = tuple(case.case_id for case in self.cases)
        operations = tuple(case.operation for case in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("E0 case IDs must be unique")
        if len(operations) != len(set(operations)):
            raise ValueError("E0 case operations must be unique")
        if set(self.base_input) != {"bundle_sha256", "fixture_id"}:
            raise ValueError("E0 base input must bind fixture_id and bundle_sha256")
        return self


class E0Observation(_EvalModel):
    case_id: str
    case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_status: Literal["pass", "blocked"]
    observed_reason_codes: tuple[str, ...]
    matched_expected: bool
    detail: str


def _load_suite() -> tuple[E0Suite, str]:
    payload = SUITE_PATH.read_bytes()
    observed = hashlib.sha256(payload).hexdigest()
    expected = SUITE_SHA_PATH.read_text(encoding="utf-8").strip()
    assert observed == expected
    suite = E0Suite.model_validate_json(payload)
    assert _populated_bundle().bundle_sha256 == suite.base_input["bundle_sha256"]
    return suite, observed


def _evaluator_source_binding() -> dict[str, object]:
    files = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in EVALUATOR_SOURCE_PATHS
    ]
    return {
        "files": files,
        "sha256": sha256_hex(files),
    }


def _project(bundle: ResearchEvidenceBundle, *, session_id: str = "e0-session"):
    return external_bundle_from_research_evidence(
        bundle,
        question="Evaluate the immutable MVP-401 evidence boundary",
        session_id=session_id,
        route=AgentRoute.EXTERNAL,
        built_at=datetime(2026, 10, 24, 21, tzinfo=UTC),
    )


def _make_prior(event_date: date, fiscal_period: str) -> PriorContextCardEvidence:
    event_at = datetime.combine(event_date, time(20), tzinfo=UTC)
    generated_at = event_at + timedelta(days=1)
    payload = _minimal_card()
    payload["identity"]["card"].update(
        {
            "as_of": generated_at.isoformat(),
            "generated_at": generated_at.isoformat(),
            "status": "approved",
        }
    )
    payload["identity"]["event"] = {
        "event_datetime": event_at.isoformat(),
        "event_datetime_status": "confirmed",
        "fiscal_period": fiscal_period,
        "timezone": "UTC",
        "type": "earnings",
    }
    payload["temporal_state"]["historical_event_cutoff"] = (
        event_at - timedelta(days=1)
    ).isoformat()
    payload["provenance"]["generated_at"] = generated_at.isoformat()
    card = ContextCardV4.model_validate(payload)
    card_sha = sha256_hex(card.model_dump(mode="json"))
    event_id = f"DEMO:{event_date.isoformat()}:{fiscal_period}"
    return PriorContextCardEvidence(
        prior_event_id=event_id,
        card_id=f"card:{event_id}",
        event_date=event_date,
        knowledge_cutoff_at=card.identity.card.as_of,
        card=card,
        card_sha256=card_sha,
        receipt=EvidenceReceipt(
            component="prior_context_card",
            receipt_id=f"receipt:{event_id}",
            content_sha256=card_sha,
            available_at=card.identity.card.as_of,
            source_ref=f"eventlens-context-card:card:{event_id}",
        ),
    )


def _three_priors() -> tuple[PriorContextCardEvidence, ...]:
    return tuple(
        _make_prior(day, period)
        for day, period in (
            (date(2026, 1, 5), "FY2025Q4"),
            (date(2026, 3, 2), "FY2026Q1"),
            (date(2026, 6, 1), "FY2026Q2"),
        )
    )


def _complete_projection(case: E0Case) -> tuple[str, ...]:
    facts = _populated_bundle()
    with patch.object(
        socket,
        "create_connection",
        side_effect=AssertionError("E0 projection attempted a network call"),
    ):
        first = _project(facts)
        second = _project(facts)
    evidence_kinds = {item.evidence_kind for item in first.evidence}
    metric_ids = {item.metric_id for item in first.canonical_metrics}
    assert set(case.required_evidence_kinds) <= evidence_kinds
    assert set(case.required_metric_ids) <= metric_ids
    assert first.unavailable_stages == ()
    assert first.canonical_bytes() == second.canonical_bytes()
    passage_ids = {item.passage_id for item in first.evidence}
    for claim in (item for item in first.evidence if item.evidence_kind == "news.claim"):
        assert set(json.loads(claim.typed_summary or "{}")["projected_citation_ids"]) <= passage_ids
    return (
        "all_components_projected",
        "citations_resolved",
        "network_calls_zero",
        "projection_deterministic",
    )


def _legacy_v1(_: E0Case) -> tuple[str, ...]:
    legacy = _bundle().model_dump(mode="json")
    legacy["schema_version"] = "eventlens.research-evidence.v1"
    legacy.pop("newly_covered_issuer")
    legacy["bundle_sha256"] = sha256_hex(
        {key: value for key, value in legacy.items() if key != "bundle_sha256"}
    )
    with TemporaryDirectory() as directory:
        path = Path(directory) / "legacy-v1.json"
        path.write_bytes(canonical_json_bytes(legacy) + b"\n")
        assert ResearchEvidenceStore().read_legacy_v1_payload(path) == legacy
        try:
            ResearchEvidenceStore().read(path)
        except ValueError as error:
            assert "schema_version" in str(error)
        else:
            raise AssertionError("legacy v1 entered the v2 reader")
    return ("legacy_checksum_verified", "legacy_v1_rejected_for_synthesis")


def _rights_degraded(_: E0Case) -> tuple[str, ...]:
    headline = _result(_request(ContentRetentionPolicy.METADATA_ONLY))
    summary = _result(_request(ContentRetentionPolicy.METADATA_ONLY, summary=True))
    observations = []
    for label, result in (("headline", headline), ("summary", summary)):
        signal = aggregate_news_signal((result,), cutoff_at=NEWS_CUTOFF)
        news = _evidence_for_news_signal(signal, cutoff=NEWS_CUTOFF)
        base = _populated_bundle()
        facts = ResearchEvidenceBundle.model_validate(
            base.model_copy(update={"bundle_sha256": None, "news": news}).model_dump(
                mode="json"
            )
        )
        observations.append((label, _project(facts, session_id=f"rights-{label}")))
    headline_projection = observations[0][1]
    headline_classification = next(
        item
        for item in headline_projection.evidence
        if item.evidence_kind == "news.classification"
    )
    assert headline_classification.rights_status.value == "metadata_only"
    assert headline_classification.retention_status.value == "headline_only"
    assert not any(
        item.evidence_kind in {"news.claim", "news.selected_passage"}
        for item in headline_projection.evidence
    )
    summary_projection = observations[1][1]
    summary_ids = {item.passage_id for item in summary_projection.evidence}
    summary_claim = next(
        item for item in summary_projection.evidence if item.evidence_kind == "news.claim"
    )
    assert set(
        json.loads(summary_claim.typed_summary or "{}")["projected_citation_ids"]
    ) <= summary_ids
    assert any(
        item.evidence_kind == "news.provider_summary"
        for item in summary_projection.evidence
    )
    return (
        "headline_only_not_content_scored",
        "summary_only_citations_resolve",
    )


def _history_three_approved(_: E0Case) -> tuple[str, ...]:
    base = _bundle()
    bundle = assemble_research_evidence_bundle(
        bundle_id="e0-three-priors",
        target=base.target,
        information_cutoff_at=base.information_cutoff_at,
        assembled_at=base.assembled_at,
        company_context=base.company_context,
        prior_cards=_three_priors(),
        missingness=tuple(
            item for item in base.missingness if item.component != "prior_cards"
        ),
    )
    assert len(bundle.prior_cards) == 3
    assert bundle.newly_covered_issuer is None
    return ("three_approved_history_valid",)


def _newly_covered_valid(_: E0Case) -> tuple[str, ...]:
    bundle = ResearchEvidenceBundle.model_validate(_bundle().model_dump(mode="json"))
    assert bundle.newly_covered_issuer is not None
    return ("newly_covered_exception_valid",)


def _mutated_bundle_checksum(_: E0Case) -> tuple[str, ...]:
    payload = _populated_bundle().model_dump(mode="json")
    payload["bundle_sha256"] = "0" * 64
    ResearchEvidenceBundle.model_validate(payload)
    raise AssertionError("mutated bundle checksum was accepted")


def _historical_checksum_mutation(_: E0Case) -> tuple[str, ...]:
    facts = _populated_bundle()
    assert facts.historical_analytics is not None
    historical = facts.historical_analytics.model_copy(
        update={"report_sha256": "0" * 64}
    )
    forged = facts.model_copy(
        update={"bundle_sha256": None, "historical_analytics": historical}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("mutated historical checksum was accepted")


def _late_evidence(_: E0Case) -> tuple[str, ...]:
    facts = _populated_bundle()
    company_context = facts.company_context.model_copy(
        update={
            "receipt": facts.company_context.receipt.model_copy(
                update={"available_at": facts.information_cutoff_at}
            )
        }
    )
    forged = facts.model_copy(
        update={"bundle_sha256": None, "company_context": company_context}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("late evidence was accepted")


def _bad_citation(_: E0Case) -> tuple[str, ...]:
    payload = _populated_bundle().model_dump(mode="json")
    classification = payload["news"]["signal"]["classifications"][0]
    classification["cited_claims"][0]["citations"][0][
        "passage_id"
    ] = "unknown-passage"
    classification["result_sha256"] = None
    payload["news"]["signal"]["signal_sha256"] = None
    payload["bundle_sha256"] = None
    ResearchEvidenceBundle.model_validate(payload)
    raise AssertionError("unresolved citation was accepted")


def _history_draft_card(_: E0Case) -> tuple[str, ...]:
    facts = _populated_bundle()
    prior = facts.prior_cards[0]
    draft_card = prior.card.model_copy(
        update={
            "identity": prior.card.identity.model_copy(
                update={
                    "card": prior.card.identity.card.model_copy(
                        update={"status": "draft"}
                    )
                }
            )
        }
    )
    draft_sha = sha256_hex(draft_card.model_dump(mode="json"))
    draft_prior = prior.model_copy(
        update={
            "card": draft_card,
            "card_sha256": draft_sha,
            "receipt": prior.receipt.model_copy(
                update={"content_sha256": draft_sha}
            ),
        }
    )
    forged = facts.model_copy(
        update={"bundle_sha256": None, "prior_cards": (draft_prior,)}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("draft history card was accepted")


def _history_future_card(_: E0Case) -> tuple[str, ...]:
    facts = _populated_bundle()
    future = _make_prior(facts.target.event_date, facts.target.fiscal_period)
    forged = facts.model_copy(
        update={"bundle_sha256": None, "prior_cards": (future,)}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("future history card was accepted")


def _history_authorization_missing(_: E0Case) -> tuple[str, ...]:
    facts = _bundle()
    forged = facts.model_copy(
        update={"bundle_sha256": None, "newly_covered_issuer": None}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("missing history authorization was accepted")


def _history_authorization_ticker_mismatch(_: E0Case) -> tuple[str, ...]:
    facts = _bundle()
    authorization = NewlyCoveredIssuerEligibility(
        ticker="OTHER",
        approved_by="test-owner",
        approved_at=facts.information_cutoff_at - timedelta(seconds=1),
        reason="other issuer authorization",
        source_ref="test-approval:other",
    )
    forged = facts.model_copy(
        update={"bundle_sha256": None, "newly_covered_issuer": authorization}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("mismatched history authorization was accepted")


def _history_authorization_late(_: E0Case) -> tuple[str, ...]:
    facts = _bundle()
    authorization = NewlyCoveredIssuerEligibility(
        ticker=facts.target.ticker,
        approved_by="test-owner",
        approved_at=facts.information_cutoff_at,
        reason="late authorization",
        source_ref="test-approval:late",
    )
    forged = facts.model_copy(
        update={"bundle_sha256": None, "newly_covered_issuer": authorization}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("late history authorization was accepted")


def _history_exception_with_three_cards(_: E0Case) -> tuple[str, ...]:
    facts = _bundle()
    forged = facts.model_copy(
        update={"bundle_sha256": None, "prior_cards": _three_priors()}
    )
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("newly-covered exception with three cards was accepted")


def _forged_context(*, ticker: str | None = None, cutoff_delta: int = 0):
    facts = _populated_bundle()
    context = facts.company_context.context.model_copy(
        update={
            "ticker": ticker or facts.target.ticker,
            "information_cutoff_at": (
                facts.company_context.context.information_cutoff_at
                + timedelta(seconds=cutoff_delta)
            ),
        }
    )
    context_sha = sha256_hex(
        context.model_dump(mode="json", exclude={"content_sha256"})
    )
    context = context.model_copy(update={"content_sha256": context_sha})
    company_context = facts.company_context.model_copy(
        update={
            "context": context,
            "receipt": facts.company_context.receipt.model_copy(
                update={"content_sha256": context_sha}
            ),
        }
    )
    return facts.model_copy(
        update={"bundle_sha256": None, "company_context": company_context}
    )


def _identity_ticker_mismatch(_: E0Case) -> tuple[str, ...]:
    forged = _forged_context(ticker="OTHER")
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("ticker identity mismatch was accepted")


def _identity_cutoff_mismatch(_: E0Case) -> tuple[str, ...]:
    forged = _forged_context(cutoff_delta=-1)
    ResearchEvidenceBundle.model_validate(forged.model_dump(mode="json"))
    raise AssertionError("cutoff identity mismatch was accepted")


OPERATIONS = {
    "bad_citation": _bad_citation,
    "complete_projection": _complete_projection,
    "history_authorization_late": _history_authorization_late,
    "history_authorization_missing": _history_authorization_missing,
    "history_authorization_ticker_mismatch": _history_authorization_ticker_mismatch,
    "history_draft_card": _history_draft_card,
    "history_exception_with_three_cards": _history_exception_with_three_cards,
    "history_future_card": _history_future_card,
    "history_three_approved": _history_three_approved,
    "historical_checksum_mutation": _historical_checksum_mutation,
    "identity_cutoff_mismatch": _identity_cutoff_mismatch,
    "identity_ticker_mismatch": _identity_ticker_mismatch,
    "late_evidence": _late_evidence,
    "legacy_v1": _legacy_v1,
    "mutated_bundle_checksum": _mutated_bundle_checksum,
    "newly_covered_valid": _newly_covered_valid,
    "rights_degraded": _rights_degraded,
}


ERROR_CODES = (
    (
        "newly-covered issuer eligibility is invalid with three prior cards",
        "history_exception_with_three_cards",
    ),
    (
        "newly-covered issuer eligibility ticker must match target",
        "history_authorization_ticker_mismatch",
    ),
    (
        "newly-covered issuer approval must precede information cutoff",
        "history_authorization_late",
    ),
    (
        "insufficient_history requires explicit newly-covered issuer eligibility",
        "history_authorization_missing",
    ),
    ("prior context cards must be approved", "history_card_not_approved"),
    ("prior-card event date must precede", "history_future_event"),
    ("analytics component checksums", "historical_checksum_mismatch"),
    ("classified claim citation must reference", "unresolved_citation"),
    (
        "company context ticker and cutoff must match",
        "company_context_identity_mismatch",
    ),
    ("evidence receipts must be strictly before", "late_evidence"),
    ("bundle_sha256 does not match", "bundle_checksum_mismatch"),
)


def _reason_code(error: Exception) -> str:
    message = str(error)
    return next((code for fragment, code in ERROR_CODES if fragment in message), "unexpected_error")


def _run_case(case: E0Case) -> E0Observation:
    try:
        codes = tuple(sorted(OPERATIONS[case.operation](case)))
        status: Literal["pass", "blocked"] = "pass"
        detail = "production contracts accepted the valid case"
    except (AssertionError, ValueError) as error:
        codes = (_reason_code(error),)
        status = "blocked"
        if isinstance(error, ValidationError):
            detail = str(error.errors()[0]["msg"]).removeprefix("Value error, ")
        else:
            detail = str(error)
    return E0Observation(
        case_id=case.case_id,
        case_sha256=sha256_hex(case.model_dump(mode="json")),
        observed_status=status,
        observed_reason_codes=codes,
        matched_expected=(
            status == case.expected_status and codes == case.expected_reason_codes
        ),
        detail=detail,
    )


def _run_suite(suite: E0Suite) -> tuple[E0Observation, ...]:
    assert set(OPERATIONS) == {case.operation for case in suite.cases}
    return tuple(_run_case(case) for case in suite.cases)


def _retained_case_results(
    suite: E0Suite, observations: tuple[E0Observation, ...]
) -> list[dict[str, object]]:
    observed_by_id = {item.case_id: item for item in observations}
    return [
        {
            "case_id": case.case_id,
            "case_sha256": observed_by_id[case.case_id].case_sha256,
            "critical": case.critical,
            "expected_status": case.expected_status,
            "observed_status": observed_by_id[case.case_id].observed_status,
            "expected_reason_codes": list(case.expected_reason_codes),
            "observed_reason_codes": list(
                observed_by_id[case.case_id].observed_reason_codes
            ),
            "matched_expected": observed_by_id[case.case_id].matched_expected,
            "detail": observed_by_id[case.case_id].detail,
        }
        for case in suite.cases
    ]


def _render_result_markdown(document: dict[str, object]) -> str:
    counts = document["case_counts"]
    execution = document["execution"]
    evaluator_source = document["evaluator_source"]
    assert (
        isinstance(counts, dict)
        and isinstance(execution, dict)
        and isinstance(evaluator_source, dict)
    )
    lines = [
        "# E0 MVP-401 research-evidence evaluation",
        "",
        f"**Status:** {str(document['status']).upper()}",
        "",
        f"- Suite: `{document['suite_id']}` (`{document['suite_sha256']}`)",
        f"- Evaluator: `{document['evaluator_version']}`",
        f"- Evaluator source: `{evaluator_source['sha256']}`",
        f"- Production boundary: `{document['production_boundary_commit']}`",
        f"- Execution base: `{document['execution_base_commit']}`",
        f"- Evaluated at: `{document['evaluated_at']}`",
        "",
        "## Result",
        "",
        (
            f"{counts['matched']}/{counts['total']} cases matched their independent "
            f"labels; critical failures: {counts['critical_failures']}."
        ),
        "",
        "| Case | Expected | Observed | Reason codes |",
        "|---|---|---|---|",
    ]
    case_results = document["case_results"]
    assert isinstance(case_results, list)
    for result in case_results:
        assert isinstance(result, dict)
        reasons = ", ".join(f"`{item}`" for item in result["observed_reason_codes"])
        lines.append(
            f"| `{result['case_id']}` | {result['expected_status']} | "
            f"{result['observed_status']} | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## Execution",
            "",
            f"- Command: `{execution['command']}`",
            f"- Runtime: {execution['runtime_seconds']} seconds",
            f"- Provider calls: {execution['provider_calls']}",
            f"- Model calls: {execution['model_calls']}",
            f"- Cache state: {execution['cache_state']}",
            f"- Reported cost: ${execution['reported_cost_usd']}",
            "",
            "## Limitations",
            "",
        ]
    )
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    lines.extend(f"- {item}" for item in limitations)
    lines.extend(
        [
            "",
            (
                "Decision 0011 is supported for the bounded MVP-401 projection and "
                "history contract. This result does not accept later synthesis, live "
                "collection, packet QA, or external-ready journey work."
            ),
            "",
            f"Content checksum: `{document['content_sha256']}`",
            "",
        ]
    )
    return "\n".join(lines)


def test_e0_suite_is_checksum_bound_and_all_cases_match_expected_labels() -> None:
    suite, _ = _load_suite()
    with patch.object(
        socket,
        "create_connection",
        side_effect=AssertionError("E0 suite attempted a network call"),
    ):
        observations = _run_suite(suite)

    assert len(observations) == 17
    assert all(observation.matched_expected for observation in observations), [
        observation.model_dump(mode="json")
        for observation in observations
        if not observation.matched_expected
    ]
    assert not {
        observation.case_id
        for observation in observations
        if observation.observed_reason_codes == ("unexpected_error",)
    }


def test_e0_suite_has_zero_network_or_model_dependencies() -> None:
    suite, _ = _load_suite()
    assert all(case.critical for case in suite.cases)
    assert "network_calls_zero" in next(
        case.expected_reason_codes
        for case in suite.cases
        if case.case_id == "complete_projection"
    )
    assert set(OPERATIONS) == {case.operation for case in suite.cases}


def test_e0_fixture_is_compact_rights_safe_and_uses_synthetic_identity() -> None:
    suite, _ = _load_suite()
    serialized = SUITE_PATH.read_text(encoding="utf-8")
    assert SUITE_PATH.stat().st_size < 12_000
    assert suite.base_input["fixture_id"].startswith("synthetic-")
    assert "DEMO" not in serialized
    assert "/Users/" not in serialized
    assert "api_key" not in serialized.casefold()
    assert NewsClassificationResult.__name__ not in serialized


def test_retained_e0_result_matches_live_observations_and_markdown() -> None:
    suite, suite_sha = _load_suite()
    observations = _run_suite(suite)
    document = json.loads(RESULT_PATH.read_text(encoding="utf-8"))

    assert set(document) == {
        "case_counts",
        "case_results",
        "content_sha256",
        "evaluated_at",
        "evaluator_source",
        "evaluator_version",
        "execution",
        "execution_base_commit",
        "limitations",
        "production_boundary_commit",
        "result_id",
        "schema_version",
        "status",
        "suite_id",
        "suite_sha256",
    }
    assert document["schema_version"] == "eventlens.external-ready-e0-result.v1"
    assert document["suite_id"] == suite.suite_id
    assert document["suite_sha256"] == suite_sha
    assert document["evaluator_version"] == suite.evaluator_version
    assert document["evaluator_source"] == _evaluator_source_binding()
    assert document["production_boundary_commit"] == suite.production_boundary_commit
    assert document["case_results"] == _retained_case_results(suite, observations)
    assert document["case_counts"] == {
        "critical_failures": 0,
        "matched": 17,
        "total": 17,
    }
    assert document["status"] == "pass"
    assert document["execution"]["provider_calls"] == 0
    assert document["execution"]["model_calls"] == 0
    expected_checksum = sha256_hex(
        {key: value for key, value in document.items() if key != "content_sha256"}
    )
    assert document["content_sha256"] == expected_checksum
    assert RESULT_MD_PATH.read_text(encoding="utf-8") == _render_result_markdown(
        document
    )
