"""Constrained, deterministic scenario analysis (not order generation)."""

from __future__ import annotations

from decimal import Decimal, localcontext

from .models import ScenarioAsset, ScenarioOptimizationResult


def optimize_worst_case_scenario(
    assets: tuple[ScenarioAsset, ...], *, concentration_limit: Decimal,
    max_turnover: Decimal,
) -> ScenarioOptimizationResult:
    """Apply a conservative per-asset worst-case ranking heuristic.

    This does not solve the coupled maximin portfolio problem and must not be
    presented as an optimum. It is reproducible scenario exploration only.
    """

    if not assets:
        raise ValueError("at least one scenario asset is required")
    if not Decimal("0") < concentration_limit <= Decimal("1"):
        raise ValueError("concentration_limit must be in (0, 1]")
    if max_turnover < 0:
        raise ValueError("max_turnover cannot be negative")
    scenario_counts = {len(asset.scenario_returns_pct) for asset in assets}
    if len(scenario_counts) != 1:
        raise ValueError("all assets must contain the same scenarios")
    if len({asset.asset_id for asset in assets}) != len(assets):
        raise ValueError("scenario asset IDs must be unique")
    if sum((asset.current_weight for asset in assets), Decimal("0")) > Decimal("1"):
        raise ValueError("current scenario weights cannot exceed 100%")
    ranked = sorted(assets, key=lambda asset: (-min(asset.scenario_returns_pct), asset.asset_id))
    remaining = Decimal("1")
    target: dict[str, Decimal] = {asset.asset_id: Decimal("0") for asset in assets}
    for asset in ranked:
        cap = min(asset.max_weight, asset.liquidity_limit, concentration_limit, remaining)
        target[asset.asset_id] = cap
        remaining -= cap
        if remaining <= 0:
            break
    raw_turnover = sum(abs(target[asset.asset_id] - asset.current_weight) for asset in assets)
    if raw_turnover > max_turnover and raw_turnover > 0:
        # Interpolate toward the target, retaining all stated bounds and the
        # cash-compatible total-weight constraint.
        fraction = max_turnover / raw_turnover
        weights = {
            asset.asset_id: asset.current_weight
            + ((target[asset.asset_id] - asset.current_weight) * fraction)
            for asset in assets
        }
    else:
        weights = target
    turnover = sum(abs(weights[asset.asset_id] - asset.current_weight) for asset in assets)
    with localcontext() as context:
        context.prec = 28
        scenario_returns = [
            sum(weights[asset.asset_id] * asset.scenario_returns_pct[index] for asset in assets)
            for index in range(len(assets[0].scenario_returns_pct))
        ]
    return ScenarioOptimizationResult(
        weights=tuple(sorted(weights.items())),
        expected_worst_case_return_pct=min(scenario_returns),
        turnover=turnover,
        max_turnover=max_turnover,
        concentration_limit=concentration_limit,
        cash_weight=Decimal("1") - sum(weights.values(), Decimal("0")),
    )
