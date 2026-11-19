from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from eventlens.agents import (
    DEFAULT_SKILLS,
    DEFAULT_TOOLS,
    AgentErrorCode,
    AnalystInterpretationRequest,
    Citation,
    ClaimKind,
    DeepSeekReasoningAdapter,
    DeterministicFixtureReasoningAdapter,
    DurableCachedReasoningAdapter,
    EvidenceClaim,
    EvidenceItem,
    EvidenceOrigin,
    MoonshotKimiReasoningAdapter,
    ReportKind,
    ReasoningProviderSettings,
    bootstrap_reasoning_adapter,
)
from eventlens.agents.models import AgentReportDraft
from eventlens.agents.prompts import build_messages
from eventlens.agents.tools import build_vibe_bundle_evidence


def _request(**changes: object) -> AnalystInterpretationRequest:
    values: dict[str, object] = {
        "request_id": "interpret-demo-1",
        "session_id": "session-demo-1",
        "event_id": "demo-2026-04-29",
        "ticker": "demo",
        "kind": ReportKind.POST_EARNINGS_REVIEW,
        "question": "What did the supplied evidence say about cloud demand?",
        "prompt_version": "agent-v1",
        "allowed_skill_ids": ("evidence_synthesis", "caveat_identification"),
        "allowed_tool_ids": ("read_evidence_passage",),
        "evidence": (
            EvidenceItem(
                source_id="transcript-q2",
                passage_id="p-17",
                origin=EvidenceOrigin.TRANSCRIPT,
                text="Management said cloud demand remained resilient in the quarter.",
                available_at=datetime(2026, 4, 29, 21, tzinfo=timezone.utc),
            ),
        ),
    }
    values.update(changes)
    return AnalystInterpretationRequest(**values)


def _completion() -> dict[str, object]:
    return {
        "headline": "Cloud-demand evidence",
        "executive_summary": "The supplied transcript passage describes resilient cloud demand.",
        "executive_summary_citations": [
            {"source_id": "transcript-q2", "passage_id": "p-17"}
        ],
        "claims": [
            {
                "kind": "fact",
                "text": "Management described cloud demand as resilient.",
                "citations": [{"source_id": "transcript-q2", "passage_id": "p-17"}],
            }
        ],
        "caveats": ["This conclusion is limited to one supplied passage."],
        "follow_up_questions": ["Which product areas drove the resilience?"],
    }


def test_fixture_adapter_is_deterministic_grounded_and_report_first() -> None:
    adapter = DeterministicFixtureReasoningAdapter(
        skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS,
        clock=lambda: datetime(2026, 5, 1, tzinfo=timezone.utc),
    )

    result = asyncio.run(adapter.interpret(_request()))

    assert result.report is not None
    assert result.report.provider == "fixture"
    assert result.report.claims[0].citations[0].passage_id == "p-17"
    assert result.report.report_id == "interpret-demo-1:report"


def test_normalized_vibe_bundle_becomes_citable_agent_evidence() -> None:
    cutoff = datetime(2026, 4, 29, 19, tzinfo=timezone.utc)
    evidence = build_vibe_bundle_evidence(
        [
            {
                "source_id": "vibe:get_sec_filings:abc",
                "passage_id": "filing-1",
                "tool_name": "get_sec_filings",
                "text": "The filing described infrastructure commitments.",
                "available_at": "2026-04-28T15:00:00Z",
                "source_identifier": "abc",
            }
        ],
        information_cutoff_at=cutoff,
    )

    assert evidence[0].origin is EvidenceOrigin.VIBE_TRADING
    assert evidence[0].available_at == datetime(
        2026, 4, 28, 15, tzinfo=timezone.utc
    )
    assert evidence[0].passage_id == "filing-1"


def test_follow_up_requires_a_prior_report() -> None:
    with pytest.raises(ValidationError, match="requires prior_report_id"):
        _request(kind=ReportKind.FOLLOW_UP, follow_up_question="What changed?")

    request = _request(
        kind=ReportKind.FOLLOW_UP,
        prior_report_id="interpret-demo-1:report",
        follow_up_question="What changed?",
    )
    assert request.follow_up_question == "What changed?"


def test_follow_up_prompt_carries_continuity_context_and_depth_requirements() -> None:
    request = _request(
        kind=ReportKind.FOLLOW_UP,
        prior_report_id="interpret-demo-1:report",
        follow_up_question="What changed?",
        allowed_skill_ids=(
            "evidence_synthesis",
            "caveat_identification",
            "follow_up_drilldown",
        ),
        metadata={
            "locked_hypothesis": {"base_case": "Demand remains durable."},
            "approved_prior_report": {
                "summary": "Capacity constrained conversion.",
                "caveats": ["Only one transcript passage was retained."],
            },
        },
    )

    system, user = build_messages(
        request, skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS
    )
    payload = json.loads(user["content"])

    assert "senior research analyst" in system["content"]
    assert payload["task"].startswith("Answer the follow-up directly")
    assert payload["continuity_context"]["approved_prior_report"]["summary"] == (
        "Capacity constrained conversion."
    )
    assert payload["continuity_context"]["locked_hypothesis"]["base_case"] == (
        "Demand remains durable."
    )
    assert any(
        "Avoid repeating the approved prior report" in item
        for item in payload["analysis_requirements"]
    )
    assert any("reaction-forecast accuracy" in item for item in payload["analysis_requirements"])
    assert any("after-hours" in item and "next-session open" in item for item in payload["analysis_requirements"])
    assert "eventlens-context-card:<card_id>" in system["content"]
    follow_up_skill = next(
        skill for skill in payload["allowed_skills"]
        if skill["skill_id"] == "follow_up_drilldown"
    )
    assert "continuity context" in follow_up_skill["description"]


def test_claim_rejects_canonical_metrics_and_trading_advice() -> None:
    with pytest.raises(ValidationError, match="canonical"):
        EvidenceClaim(
            kind=ClaimKind.FACT,
            text="The expected move is 8%.",
            citations=(Citation(source_id="s", passage_id="p"),),
        )
    for text in (
        "Purchase shares before earnings.",
        "Accumulate the stock.",
        "I recommend owning calls.",
    ):
        with pytest.raises(ValidationError, match="trading"):
            EvidenceClaim(
                kind=ClaimKind.INTERPRETATION,
                text=text,
                citations=(Citation(source_id="s", passage_id="p"),),
            )
    with pytest.raises(ValidationError, match="trading"):
        EvidenceClaim(
            kind=ClaimKind.INTERPRETATION,
            text="Buy the stock before earnings.",
            citations=(Citation(source_id="s", passage_id="p"),),
        )


def test_fixture_rejects_unknown_citation() -> None:
    fixture = AgentReportDraft.model_validate(_completion() | {
        "claims": [{"kind": "fact", "text": "Unsupported", "citations": [{"source_id": "other", "passage_id": "p"}]}]
    })
    adapter = DeterministicFixtureReasoningAdapter(
        fixtures={"interpret-demo-1": fixture}, skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS,
    )

    result = asyncio.run(adapter.interpret(_request()))

    assert result.report is None
    assert result.errors[0].code is AgentErrorCode.INVALID_EVIDENCE


def test_fixture_rejects_unallowlisted_tools_before_interpretation() -> None:
    adapter = DeterministicFixtureReasoningAdapter(skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS)

    result = asyncio.run(adapter.interpret(_request(allowed_tool_ids=("execute_trade",))))

    assert result.report is None
    assert result.errors[0].code is AgentErrorCode.INVALID_EVIDENCE


def test_deepseek_adapter_uses_injected_http_json_and_never_puts_secret_in_prompt() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["Authorization"]
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(_completion())}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekReasoningAdapter(
        api_key=SecretStr("top-secret-key"), model="deepseek-v4-pro", skills=DEFAULT_SKILLS,
        tools=DEFAULT_TOOLS, http=client,
    )

    result = asyncio.run(adapter.interpret(_request()))
    asyncio.run(client.aclose())

    assert result.report is not None
    assert result.provider == "deepseek"
    assert observed["authorization"] == "Bearer top-secret-key"
    assert "top-secret-key" not in json.dumps(observed["body"])
    assert observed["body"]["thinking"] == {"type": "enabled"}
    assert observed["body"]["reasoning_effort"] == "high"
    assert observed["body"]["max_tokens"] == 4000


def test_deepseek_repairs_one_schema_failure_with_valid_citation_allowlist() -> None:
    bodies: list[dict[str, object]] = []
    responses = iter(
        [
            httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}),
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(_completion())}}]},
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return next(responses)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekReasoningAdapter(
        api_key=SecretStr("secret"), model="deepseek-v4-pro",
        skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS, http=client, max_attempts=2,
    )
    result = asyncio.run(adapter.interpret(_request()))
    asyncio.run(client.aclose())

    assert result.report is not None
    assert result.attempts == 2
    repair = json.loads(bodies[1]["messages"][-1]["content"])
    assert repair["failure_category"] == "schema_validation"
    assert {item["location"] for item in repair["validation_issues"]} >= {
        "headline", "executive_summary", "claims"
    }
    assert repair["completion_metadata"] == {
        "content_characters": 2,
        "finish_reason": "unavailable",
        "starts_code_fence": False,
        "starts_json_object": True,
    }
    assert {"source_id": "transcript-q2", "passage_id": "p-17"} in repair["valid_citations"]


def test_bootstrap_preserves_deepseek_v4_pro_reasoning_configuration() -> None:
    adapter = bootstrap_reasoning_adapter(
        ReasoningProviderSettings(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_key=SecretStr("deepseek-secret"),
            base_url="https://api.deepseek.com/v1",
            timeout_seconds=17,
            max_attempts=2,
            max_tokens=1234,
        )
    )

    try:
        assert isinstance(adapter, DeepSeekReasoningAdapter)
        assert adapter.provider == "deepseek"
        assert adapter.model == "deepseek-v4-pro"
        assert adapter._request_overrides == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        assert "deepseek-secret" not in repr(adapter)
    finally:
        asyncio.run(adapter.aclose())


def test_openai_compatible_retry_and_secret_safe_error_taxonomy() -> None:
    observed: dict[str, object] = {}
    responses = iter([httpx.Response(429), httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(_completion())}}]})])
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed["body"] = json.loads(request.content)
        return next(responses)

    async def sleeper(value: float) -> None:
        delays.append(value)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MoonshotKimiReasoningAdapter(
        api_key=SecretStr("moonshot-secret"), model="kimi", skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS,
        http=client, sleeper=sleeper,
    )
    result = asyncio.run(adapter.interpret(_request()))
    asyncio.run(client.aclose())

    assert result.report is not None
    assert result.attempts == 2
    assert delays == [0.25]
    assert observed["body"]["max_completion_tokens"] == 4000
    assert "max_tokens" not in observed["body"]

    failing_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="moonshot-secret")))
    failing = MoonshotKimiReasoningAdapter(
        api_key=SecretStr("moonshot-secret"), model="kimi", skills=DEFAULT_SKILLS, tools=DEFAULT_TOOLS,
        http=failing_client,
    )
    failed = asyncio.run(failing.interpret(_request()))
    asyncio.run(failing_client.aclose())
    assert failed.errors[0].code is AgentErrorCode.AUTHENTICATION
    assert "moonshot-secret" not in failed.errors[0].message


def test_openai_compatible_timeout_is_typed_and_retryable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("do not expose transport detail")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekReasoningAdapter(
        api_key=SecretStr("deepseek-secret"), model="deepseek-chat", skills=DEFAULT_SKILLS,
        tools=DEFAULT_TOOLS, http=client, max_attempts=1,
    )
    result = asyncio.run(adapter.interpret(_request()))
    asyncio.run(client.aclose())

    assert result.report is None
    assert result.errors[0].code is AgentErrorCode.TIMEOUT
    assert result.errors[0].retryable is True


def test_keyed_reasoning_rejects_plain_http_remote_endpoint() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        DeepSeekReasoningAdapter(
            api_key=SecretStr("secret"),
            model="deepseek-v4-pro",
            base_url="http://api.example.test/v1",
            skills=DEFAULT_SKILLS,
            tools=DEFAULT_TOOLS,
        )


def test_successful_reasoning_result_survives_adapter_restart(tmp_path: Path) -> None:
    first_inner = DeterministicFixtureReasoningAdapter(
        skills=DEFAULT_SKILLS,
        tools=DEFAULT_TOOLS,
        clock=lambda: datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    first = DurableCachedReasoningAdapter(first_inner, tmp_path)
    original = asyncio.run(first.interpret(_request()))

    second_inner = DeterministicFixtureReasoningAdapter(
        skills=DEFAULT_SKILLS,
        tools=DEFAULT_TOOLS,
        clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    reopened = DurableCachedReasoningAdapter(second_inner, tmp_path)
    replayed = asyncio.run(reopened.interpret(_request()))

    assert replayed == original
    assert replayed.report is not None
    assert replayed.report.generated_at == datetime(2026, 5, 1, tzinfo=timezone.utc)
