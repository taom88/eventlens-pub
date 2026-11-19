"""Provider-neutral prompt material for constrained semantic extraction.

No provider is invoked here.  Callers can supply this text to their own model
boundary and must route its response through ``parse_extraction_output``.
"""

from __future__ import annotations

import json

from .extraction import ExtractionRequest, ExtractionResult


EXTRACTION_SYSTEM_PROMPT = """Extract only complete, source-native semantic claims.
Return JSON matching the supplied schema exactly.  Do not add facts, use only
the supplied source passage, and set every receipt span to the exact local
character range from that passage.  A claim must be a complete proposition,
not a sentence fragment.  The verbatim receipt must equal that exact span.
Do not return the source passage body outside individual bounded receipts."""


def extraction_json_schema() -> dict[str, object]:
    """Return a fresh validation schema suitable for a structured-output call."""

    return ExtractionResult.model_json_schema(mode="validation")


def build_extraction_prompt(request: ExtractionRequest) -> str:
    """Build a bounded prompt without implying any live model integration."""

    payload = {
        "source_ref": request.source_ref,
        "source_text_sha256": request.passage.text_sha256,
        "passage": request.passage.text,
        "schema": extraction_json_schema(),
    }
    return f"{EXTRACTION_SYSTEM_PROMPT}\n\nInput:\n{json.dumps(payload, ensure_ascii=False)}"


__all__ = ["EXTRACTION_SYSTEM_PROMPT", "build_extraction_prompt", "extraction_json_schema"]
