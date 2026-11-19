from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from eventlens.learning.policy import (
    PolicyCandidate,
    PolicyContext,
    PolicyMode,
    RewardComponents,
    RoutingAction,
    calculate_reward,
    policy_checksum,
    shadow_candidate_action,
)
from eventlens.learning.registry import PolicyRegistry
from eventlens.learning.validation import compare_paired_rewards


def _policy() -> PolicyCandidate:
    payload = {
        "policy_id": "challenger-v1",
        "version": "1.0",
        "created_at": "2026-07-26T00:00:00Z",
        "mode": PolicyMode.SHADOW,
        "minimum_effect_size": Decimal("0.02"),
        "maximum_cost_usd": Decimal("5"),
        "allowed_tools": ("get_sec_filings", "get_stock_news"),
        "allowed_desks": ("earnings_research", "risk_committee", "quant_strategy"),
        "seed": 42,
        "parent_policy_id": "fixed-baseline-v1",
    }
    return PolicyCandidate(
        **payload, policy_sha256=policy_checksum(payload)
    )


def _context() -> PolicyContext:
    return PolicyContext(
        sector="technology",
        event_timing="after_market_close",
        available_tools=("get_stock_news", "get_sec_filings"),
        evidence_gap_ids=("missing-options",),
        market_regime="high-volatility",
        event_complexity=4,
        remaining_cost_budget_usd=Decimal("3"),
        remaining_quota_units=1,
        historical_desk_scores={"quant_strategy": Decimal("0.8")},
    )


def test_shadow_policy_is_reproducible_and_cost_constrained() -> None:
    first = shadow_candidate_action(_policy(), _context())
    second = shadow_candidate_action(_policy(), _context())
    assert first == second
    assert first.quota_budget <= _context().remaining_quota_units
    assert first.cost_budget_usd <= _context().remaining_cost_budget_usd
    assert first.cost_budget_usd <= _policy().maximum_cost_usd
    assert first.escalate_risk is True


def test_routing_action_cannot_become_a_trading_policy() -> None:
    with pytest.raises(ValidationError, match="trading"):
        RoutingAction(
            optional_evidence_tools=("place_buy_order",),
            active_desks=("risk_committee",),
            desk_model_assignments={"risk_committee": "fixture"},
            token_budget=100,
            quota_budget=0,
        )


def test_safety_violations_dominate_reward_and_block_candidate() -> None:
    common = dict(
        citation_validity=Decimal("1"),
        factual_correctness=Decimal("1"),
        calibration=Decimal("1"),
        forecast_accuracy=Decimal("1"),
        analyst_usefulness=Decimal("1"),
        postmortem_quality=Decimal("1"),
        latency_score=Decimal("1"),
        cost_score=Decimal("1"),
        quota_score=Decimal("1"),
    )
    safe = calculate_reward(RewardComponents(**common, safety_violations=0))
    unsafe = calculate_reward(RewardComponents(**common, safety_violations=1))
    assert safe.total_reward == Decimal("1")
    assert unsafe.total_reward <= Decimal("-1000")
    assert unsafe.eligible is False


def test_registry_requires_effect_gate_human_approval_and_supports_rollback(
    tmp_path: Path,
) -> None:
    registry = PolicyRegistry(tmp_path / "registry.jsonl")
    policy = _policy()
    registry.register_challenger(policy)
    comparison = compare_paired_rewards(
        "fixed-baseline-v1",
        policy.policy_id,
        tuple(Decimal("0.5") for _ in range(50)),
        tuple(Decimal("0.55") for _ in range(50)),
        minimum_effect_size=Decimal("0.02"),
    )
    assert comparison.eligible_for_human_review
    promoted = registry.promote(
        policy,
        comparison,
        approved_by="analyst@example.com",
        current_champion_id="fixed-baseline-v1",
    )
    assert promoted.approved_by == "analyst@example.com"
    rollback = registry.rollback(
        current_policy_id=policy.policy_id,
        previous_policy_id="fixed-baseline-v1",
        approved_by="risk@example.com",
        reason="regime instability",
    )
    assert rollback.event_type == "policy_rollback"
    assert len(registry.events()) == 3


def test_tiny_or_unsafe_comparison_cannot_be_promoted(tmp_path: Path) -> None:
    policy = _policy()
    comparison = compare_paired_rewards(
        "fixed",
        policy.policy_id,
        (Decimal("0.5"),),
        (Decimal("0.9"),),
        minimum_effect_size=Decimal("0.02"),
        safety_violations=1,
    )
    assert comparison.eligible_for_human_review is False
    with pytest.raises(ValueError, match="promotion gates"):
        PolicyRegistry(tmp_path / "registry.jsonl").promote(
            policy,
            comparison,
            approved_by="human",
            current_champion_id="fixed",
        )
