"""Offline comparison, uncertainty, and off-policy evaluation contracts."""

from __future__ import annotations

import math
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PolicyComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    champion_policy_id: str
    challenger_policy_id: str
    sample_size: int = Field(ge=0)
    mean_champion_reward: Decimal
    mean_challenger_reward: Decimal
    effect_size: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    minimum_effect_size: Decimal = Field(gt=0)
    safety_violations: int = Field(ge=0)
    passes_effect_gate: bool
    eligible_for_human_review: bool
    evaluation_version: str = "paired-temporal-shadow-v1"


def compare_paired_rewards(
    champion_policy_id: str,
    challenger_policy_id: str,
    champion_rewards: tuple[Decimal, ...],
    challenger_rewards: tuple[Decimal, ...],
    *,
    minimum_effect_size: Decimal,
    safety_violations: int = 0,
    minimum_samples: int = 50,
) -> PolicyComparison:
    if len(champion_rewards) != len(challenger_rewards):
        raise ValueError("paired comparison requires equal sample counts")
    if not champion_rewards:
        raise ValueError("paired comparison requires observations")
    differences = tuple(
        challenger - champion
        for champion, challenger in zip(champion_rewards, challenger_rewards)
    )
    n = len(differences)
    effect = sum(differences, Decimal("0")) / n
    if n > 1:
        variance = sum(
            ((item - effect) ** 2 for item in differences), Decimal("0")
        ) / (n - 1)
        margin = Decimal(str(1.96 * math.sqrt(float(variance) / n)))
    else:
        margin = Decimal("1")
    lower, upper = effect - margin, effect + margin
    passes = lower >= minimum_effect_size
    eligible = n >= minimum_samples and passes and safety_violations == 0
    return PolicyComparison(
        champion_policy_id=champion_policy_id,
        challenger_policy_id=challenger_policy_id,
        sample_size=n,
        mean_champion_reward=sum(champion_rewards, Decimal("0")) / n,
        mean_challenger_reward=sum(challenger_rewards, Decimal("0")) / n,
        effect_size=effect,
        lower_bound=lower,
        upper_bound=upper,
        minimum_effect_size=minimum_effect_size,
        safety_violations=safety_violations,
        passes_effect_gate=passes,
        eligible_for_human_review=eligible,
    )
