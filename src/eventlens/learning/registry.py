"""Append-only champion/challenger registry with human promotion and rollback."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .policy import PolicyCandidate, PolicyMode
from .validation import PolicyComparison


class RegistryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str
    policy_id: str
    occurred_at: datetime
    approved_by: str | None = None
    reason: str
    previous_policy_id: str | None = None


class PolicyRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def register_challenger(self, policy: PolicyCandidate) -> RegistryEvent:
        if policy.mode is not PolicyMode.SHADOW:
            raise ValueError("new challengers must start in shadow mode")
        event = RegistryEvent(
            event_type="challenger_registered",
            policy_id=policy.policy_id,
            occurred_at=datetime.now(timezone.utc),
            reason="shadow-only challenger registered",
        )
        self._append(event)
        return event

    def promote(
        self,
        policy: PolicyCandidate,
        comparison: PolicyComparison,
        *,
        approved_by: str,
        current_champion_id: str,
    ) -> RegistryEvent:
        if policy.mode is not PolicyMode.SHADOW:
            raise ValueError("only a shadow challenger can be promoted")
        if not approved_by.strip():
            raise ValueError("human approver is required")
        if not comparison.eligible_for_human_review:
            raise ValueError("challenger did not pass offline promotion gates")
        if comparison.challenger_policy_id != policy.policy_id:
            raise ValueError("comparison challenger does not match policy")
        event = RegistryEvent(
            event_type="policy_promoted",
            policy_id=policy.policy_id,
            occurred_at=datetime.now(timezone.utc),
            approved_by=approved_by,
            reason="human-approved after temporal shadow comparison",
            previous_policy_id=current_champion_id,
        )
        self._append(event)
        return event

    def rollback(
        self,
        *,
        current_policy_id: str,
        previous_policy_id: str,
        approved_by: str,
        reason: str,
    ) -> RegistryEvent:
        if not approved_by.strip() or not reason.strip():
            raise ValueError("rollback requires human approval and reason")
        event = RegistryEvent(
            event_type="policy_rollback",
            policy_id=previous_policy_id,
            occurred_at=datetime.now(timezone.utc),
            approved_by=approved_by,
            reason=reason,
            previous_policy_id=current_policy_id,
        )
        self._append(event)
        return event

    def events(self) -> tuple[RegistryEvent, ...]:
        if not self.path.exists():
            return ()
        return tuple(
            RegistryEvent.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def _append(self, event: RegistryEvent) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")
