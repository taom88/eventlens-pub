"""Shared primitives for deterministic EventLens domain calculations."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    """Immutable, strict model used for canonical domain values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CalculationWarning(DomainModel):
    """A visible limitation or missing-data condition in a calculation result."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


ZERO = Decimal("0")
ONE = Decimal("1")
