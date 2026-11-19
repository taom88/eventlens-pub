"""Deterministic, rights-safe assembly of the R1 external-agent bundle."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .external import ExternalAgentBundle

_SECRET_KEY = re.compile(r"(?:api[_-]?key|secret|token|password|authorization)", re.I)
_LOCAL_PATH = re.compile(r"^(?:file://|/|~[/\\]|[A-Za-z]:[\\/])|(?:\.eventlens-cache|__pycache__|/tmp/)")
_FORBIDDEN_KEYS = re.compile(r"(?:actual_results?|realized_move|post_event|outcome|transcript_delta)", re.I)
_SECRET_VALUE = re.compile(r"(?:^sk-[A-Za-z0-9]|^Bearer\s+|^eyJ[A-Za-z0-9_-]+\.)")


def _reject_unsafe(value: Any, *, key: str = "") -> None:
    if isinstance(value, Mapping):
        for name, item in value.items():
            if _SECRET_KEY.search(str(name)):
                raise ValueError("external bundle cannot contain secret-bearing fields")
            if _FORBIDDEN_KEYS.search(str(name)):
                raise ValueError("external bundle cannot contain target-event outcome fields")
            _reject_unsafe(item, key=str(name))
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            _reject_unsafe(item, key=key)
        return
    if isinstance(value, str):
        if _LOCAL_PATH.search(value) or ("path" in key.casefold() and ("/" in value or "\\" in value)):
            raise ValueError("external bundle cannot contain mutable or local paths")
        if _SECRET_VALUE.search(value):
            raise ValueError("external bundle cannot contain secret-like values")


def assemble_external_bundle(values: Mapping[str, Any] | None = None, **kwargs: Any) -> ExternalAgentBundle:
    """Build a canonical bundle from normalized typed inputs.

    This function does not fetch providers or invoke an agent.  It validates
    temporal and rights boundaries, sorts only semantically unordered lists,
    and delegates the final typed/checksum validation to the frozen contract.
    """
    payload = dict(values or {})
    payload.update(kwargs)
    _reject_unsafe(payload)
    # Coerce and validate every nested mapping before inspecting or sorting it.
    # This prevents raw dictionaries from bypassing temporal and rights checks.
    validated = ExternalAgentBundle.create(**payload)
    normalized = validated.model_dump(mode="python", exclude={"content_sha256"})
    normalized["canonical_metrics"] = tuple(
        sorted(
            (
                item.model_copy(
                    update={"provenance_ids": tuple(sorted(item.provenance_ids))}
                )
                for item in validated.canonical_metrics
            ),
            key=lambda item: item.metric_id,
        )
    )
    normalized["evidence"] = tuple(
        sorted(validated.evidence, key=lambda item: (item.source_id, item.passage_id))
    )
    normalized["historical_context"] = tuple(
        sorted(
            validated.historical_context,
            key=lambda item: (item.event_date, item.card_id),
        )
    )
    for field in ("missingness", "warnings", "unavailable_stages", "forbidden_behaviors"):
        normalized[field] = tuple(sorted(set(getattr(validated, field))))
    normalized["allowed_skills"] = tuple(
        sorted(validated.allowed_skills, key=lambda item: (item.capability_id, item.version))
    )
    normalized["allowed_tools"] = tuple(
        sorted(validated.allowed_tools, key=lambda item: (item.capability_id, item.version))
    )
    assembled = ExternalAgentBundle.create(**normalized)
    _reject_unsafe(assembled.model_dump(mode="json"))
    return assembled
