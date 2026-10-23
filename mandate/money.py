"""Strict money parsing and formatting.

Money is stored as :class:`decimal.Decimal` in major units.  The parser is
deliberately strict: scientific notation, NaN/Infinity, booleans, whitespace,
commas, signs and sub-cent precision are rejected rather than silently
coerced.  That keeps the policy engine's arithmetic auditable.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "MoneyError",
    "parse_money",
    "format_money",
    "to_payload_number",
    "ZERO",
]

ZERO = Decimal("0.00")

_DECIMAL_TEXT = re.compile(r"\d+(?:\.\d{1,2})?\Z")


class MoneyError(ValueError):
    """Raised when a value cannot be represented as safe major-unit money."""


def parse_money(value: Any, *, field: str = "amount") -> Decimal:
    """Parse ``value`` into a non-negative Decimal with at most two decimals.

    Accepted inputs: ``int``, ``float`` (finite), ``Decimal`` and plain decimal
    strings such as ``"19680.00"``.  Everything else raises :class:`MoneyError`.
    """

    if isinstance(value, bool):
        raise MoneyError(f"{field} must be a money value, not a boolean")

    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise MoneyError(f"{field} must be finite")
        amount = Decimal(str(value))
    elif isinstance(value, str):
        if value != value.strip():
            raise MoneyError(f"{field} must not contain leading or trailing spaces")
        if not _DECIMAL_TEXT.fullmatch(value):
            raise MoneyError(
                f"{field} must be a plain non-negative decimal with at most two decimals"
            )
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - regex guards this
            raise MoneyError(f"{field} is not a valid decimal") from exc
    else:
        raise MoneyError(f"{field} must be int, float, Decimal or str")

    if not amount.is_finite():
        raise MoneyError(f"{field} must be finite")

    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int) or exponent > 0 or exponent < -2:
        raise MoneyError(f"{field} must use at most two decimal places and no exponent")

    if amount < 0:
        raise MoneyError(f"{field} must not be negative")

    return amount


def format_money(value: Decimal) -> str:
    """Format as a fixed two-decimal major-unit string."""

    if not isinstance(value, Decimal):
        value = parse_money(value)
    return f"{value:.2f}"


def to_payload_number(value: Decimal) -> float:
    """Convert a validated Decimal to the JSON number shape used by the card API.

    The value has already been constrained to two decimal places, so the float
    conversion is exact for every amount in the sandbox's operating range.
    """

    if not isinstance(value, Decimal):
        value = parse_money(value)
    return float(value)
