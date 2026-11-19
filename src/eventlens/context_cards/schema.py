"""JSON Schema export for the EventLens v4 context-card contract."""

from __future__ import annotations

from typing import Any

from .models import SCHEMA_VERSION, ContextCardV4

__all__ = ["SCHEMA_VERSION", "context_card_json_schema"]


def context_card_json_schema() -> dict[str, Any]:
    """Return a fresh JSON-serializable schema for the canonical v4 model."""

    return ContextCardV4.model_json_schema(
        mode="validation",
        ref_template="#/$defs/{model}",
    )
