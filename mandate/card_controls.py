"""Derive virtual-card controls from a VALID Decision Passport.

Nothing extracted from an untrusted document reaches this payload.  The
passport is validated first, currency and MCC are checked against trusted
allowlists, and the card, not the prompt, enforces the limits.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .money import MoneyError, format_money, parse_money, to_payload_number
from .passport import (
    ApprovedIntent,
    Passport,
    STATUS_VALID,
    validate_passport,
)

__all__ = [
    "CardControlError",
    "DEFAULT_ALLOWED_CURRENCIES",
    "DEFAULT_ALLOWED_MERCHANT_CATEGORIES",
    "card_controls_from_passport",
    "summarize_controls",
]

DEFAULT_ALLOWED_CURRENCIES = ("USD",)
DEFAULT_ALLOWED_MERCHANT_CATEGORIES = ("5734",)


class CardControlError(ValueError):
    """Card controls cannot be derived safely."""


def _normalise_allowlist(
    values: Iterable[str], *, field_name: str
) -> tuple[str, ...]:
    if isinstance(values, str):
        raise CardControlError(f"{field_name} must be a collection, not a string")
    try:
        normalised = tuple(str(value).strip() for value in values)
    except TypeError as exc:
        raise CardControlError(f"{field_name} must be iterable") from exc
    if not normalised:
        raise CardControlError(f"{field_name} must not be empty")
    if any(not value for value in normalised):
        raise CardControlError(f"{field_name} must not contain empty values")
    return normalised


def card_controls_from_passport(
    passport: Passport,
    key: bytes | str,
    *,
    now: datetime | None = None,
    expected_intent: ApprovedIntent | Mapping[str, Any] | None = None,
    allowed_currencies: Iterable[str] = DEFAULT_ALLOWED_CURRENCIES,
    allowed_merchant_categories: Iterable[str] = DEFAULT_ALLOWED_MERCHANT_CATEGORIES,
    max_amount: Decimal | str | int | float | None = None,
) -> dict[str, Any]:
    """Return the card-create controls payload for a valid passport."""

    currencies = _normalise_allowlist(
        allowed_currencies, field_name="allowed_currencies"
    )
    currencies = tuple(currency.upper() for currency in currencies)
    mccs = _normalise_allowlist(
        allowed_merchant_categories,
        field_name="allowed_merchant_categories",
    )

    validation = validate_passport(
        passport,
        key,
        now=now,
        expected_intent=expected_intent,
    )
    if validation.status != STATUS_VALID:
        raise CardControlError(
            f"passport is {validation.status}: {validation.reason}"
        )

    currency = passport.currency.strip().upper()
    if currency not in currencies:
        raise CardControlError(f"currency {currency!r} is not in the trusted allowlist")
    mcc = passport.merchant_category.strip()
    if mcc not in mccs:
        raise CardControlError(f"MCC {mcc!r} is not in the trusted allowlist")

    amount = parse_money(passport.amount, field="passport amount")
    if max_amount is not None:
        try:
            ceiling = parse_money(max_amount, field="max_amount")
        except MoneyError as exc:
            raise CardControlError(str(exc)) from exc
        if amount > ceiling:
            raise CardControlError(
                f"passport amount {format_money(amount)} exceeds the safety ceiling "
                f"{format_money(ceiling)}; possible unit or cents-for-dollars error"
            )

    if passport.billing_cadence == "ANNUAL":
        transaction_count = "SINGLE"
        cumulative_interval = "ALL_TIME"
    elif passport.billing_cadence == "MONTHLY":
        transaction_count = "MULTIPLE"
        cumulative_interval = "MONTHLY"
    else:  # validate_passport already rejects this, kept as a defensive check
        raise CardControlError("billing_cadence must be MONTHLY or ANNUAL")

    if not currencies or not mccs:
        raise CardControlError("card control arrays must never be empty")

    return {
        "allowed_transaction_count": transaction_count,
        "allowed_currencies": list(currencies),
        "allowed_merchant_categories": list(mccs),
        "transaction_limits": {
            "currency": currency,
            "limits": [
                {
                    "amount": to_payload_number(amount),
                    "interval": "PER_TRANSACTION",
                },
                {
                    "amount": to_payload_number(amount),
                    "interval": cumulative_interval,
                },
            ],
        },
    }


def summarize_controls(controls: Mapping[str, Any]) -> list[str]:
    """Human-readable summary used by the CLI and dashboard."""

    limits = controls.get("transaction_limits", {}).get("limits", [])
    amount = limits[0]["amount"] if limits else None
    count = controls.get("allowed_transaction_count", "?")
    cadence_interval = limits[1]["interval"] if len(limits) > 1 else "?"
    currencies = ", ".join(controls.get("allowed_currencies", []))
    mccs = ", ".join(controls.get("allowed_merchant_categories", []))
    amount_text = f"{amount:,.2f}" if isinstance(amount, (int, float)) else "?"
    return [
        f"Transaction count: {count}",
        f"Per-transaction cap: {amount_text}",
        f"Cumulative cap: {amount_text} per {cadence_interval}",
        f"Allowed currencies: {currencies}",
        f"Allowed MCCs: {mccs}",
    ]
