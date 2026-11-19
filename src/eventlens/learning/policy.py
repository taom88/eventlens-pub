"""Constrained, offline orchestration routing policy—never a trading policy."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


SAFETY_PENALTY = Decimal("1000")


class PolicyMode(StrEnum):
    SHADOW = "shadow"
    CHAMPION = "champion"
    RETIRED = "retired"


class PolicyContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sector: str | None = None
    event_timing: str
    available_tools: tuple[str, ...]
    evidence_gap_ids: tuple[str, ...] = ()
    market_regime: str | None = None
    event_complexity: int = Field(ge=1, le=5)
    remaining_cost_budget_usd: Decimal = Field(ge=0)
    remaining_quota_units: int = Field(ge=0)
    historical_desk_scores: dict[str, Decimal] = {}


class RoutingAction(BaseModel):
    """Permitted research-orchestration actions only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    optional_evidence_tools: tuple[str, ...] = ()
    active_desks: tuple[str, ...]
    desk_model_assignments: dict[str, str]
    request_additional_evidence: bool = False
    escalate_risk: bool = False
    request_analyst_clarification: bool = False
    report_depth: str = "standard"
    token_budget: int = Field(ge=0)
    quota_budget: int = Field(ge=0)
    cost_budget_usd: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def prohibit_trading_actions(self) -> "RoutingAction":
        serialized = json.dumps(self.model_dump(mode="json")).casefold()
        prohibited = ("buy", "sell", "order", "broker", "position size", "execute trade")
        if any(item in serialized for item in prohibited):
            raise ValueError("routing policy cannot contain trading or execution actions")
        return self


class RewardComponents(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_validity: Decimal = Field(ge=0, le=1)
    factual_correctness: Decimal = Field(ge=0, le=1)
    calibration: Decimal = Field(ge=0, le=1)
    forecast_accuracy: Decimal = Field(ge=0, le=1)
    analyst_usefulness: Decimal = Field(ge=0, le=1)
    postmortem_quality: Decimal = Field(ge=0, le=1)
    latency_score: Decimal = Field(ge=0, le=1)
    cost_score: Decimal = Field(ge=0, le=1)
    quota_score: Decimal = Field(ge=0, le=1)
    safety_violations: int = Field(ge=0)


class RewardResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_reward: Decimal
    eligible: bool
    safety_dominated: bool
    calculation_version: str = "multi-objective-safety-dominant-v1"


class PolicyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    created_at: str
    mode: PolicyMode = PolicyMode.SHADOW
    minimum_effect_size: Decimal = Field(default=Decimal("0.02"), gt=0)
    maximum_cost_usd: Decimal = Field(ge=0)
    allowed_tools: tuple[str, ...]
    allowed_desks: tuple[str, ...]
    seed: int
    parent_policy_id: str | None = None
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def checksum_matches_policy(self) -> "PolicyCandidate":
        payload = self.model_dump(mode="json", exclude={"policy_sha256"})
        if self.policy_sha256 != policy_checksum(payload):
            raise ValueError("policy checksum does not match versioned policy content")
        return self


def calculate_reward(components: RewardComponents) -> RewardResult:
    if components.safety_violations:
        return RewardResult(
            total_reward=-SAFETY_PENALTY * components.safety_violations,
            eligible=False,
            safety_dominated=True,
        )
    weights = {
        "citation_validity": Decimal("0.18"),
        "factual_correctness": Decimal("0.18"),
        "calibration": Decimal("0.12"),
        "forecast_accuracy": Decimal("0.10"),
        "analyst_usefulness": Decimal("0.14"),
        "postmortem_quality": Decimal("0.10"),
        "latency_score": Decimal("0.06"),
        "cost_score": Decimal("0.06"),
        "quota_score": Decimal("0.06"),
    }
    total = sum(
        (getattr(components, key) * weight for key, weight in weights.items()),
        Decimal("0"),
    )
    return RewardResult(total_reward=total, eligible=True, safety_dominated=False)


def fixed_baseline_action(context: PolicyContext) -> RoutingAction:
    desks = (
        "earnings_research",
        "global_equities",
        "quant_strategy",
        "technical_panel",
        "risk_committee",
    )
    return RoutingAction(
        optional_evidence_tools=tuple(sorted(context.available_tools)),
        active_desks=desks,
        desk_model_assignments={desk: "configured-default" for desk in desks},
        request_additional_evidence=bool(context.evidence_gap_ids),
        escalate_risk=bool(context.evidence_gap_ids),
        request_analyst_clarification=context.event_complexity >= 5,
        report_depth="deep" if context.event_complexity >= 4 else "standard",
        token_budget=min(20_000, 2_000 * len(desks)),
        quota_budget=min(context.remaining_quota_units, len(context.available_tools)),
        cost_budget_usd=context.remaining_cost_budget_usd,
    )


def shadow_candidate_action(
    policy: PolicyCandidate,
    context: PolicyContext,
) -> RoutingAction:
    """Deterministic contextual routing proposal; never applied in shadow mode."""

    ranked_desks = sorted(
        policy.allowed_desks,
        key=lambda desk: (
            -context.historical_desk_scores.get(desk, Decimal("0.5")),
            desk,
        ),
    )
    required = {"risk_committee", "earnings_research"}
    selected = tuple(
        desk
        for desk in ranked_desks
        if desk in required or context.event_complexity >= 3
    )
    tools = tuple(
        tool for tool in sorted(context.available_tools)
        if tool in set(policy.allowed_tools)
    )
    return RoutingAction(
        optional_evidence_tools=tools,
        active_desks=selected,
        desk_model_assignments={desk: "challenger-default" for desk in selected},
        request_additional_evidence=bool(context.evidence_gap_ids),
        escalate_risk=bool(context.evidence_gap_ids) or context.event_complexity >= 5,
        request_analyst_clarification=context.event_complexity >= 4,
        report_depth="deep" if context.event_complexity >= 4 else "standard",
        token_budget=min(15_000, 2_000 * len(selected)),
        quota_budget=min(context.remaining_quota_units, len(tools)),
        cost_budget_usd=min(
            context.remaining_cost_budget_usd, policy.maximum_cost_usd
        ),
    )


def policy_checksum(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
