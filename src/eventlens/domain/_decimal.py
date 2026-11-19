"""Versioned Decimal policy for reproducible canonical calculations."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Context, ROUND_HALF_EVEN, localcontext
from typing import Iterator


DECIMAL_CALCULATION_VERSION = "decimal-v1-prec28-half-even"
CANONICAL_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


@contextmanager
def canonical_decimal_context() -> Iterator[Context]:
    """Isolate arithmetic from caller or process-global Decimal settings."""

    with localcontext(CANONICAL_DECIMAL_CONTEXT) as context:
        yield context
