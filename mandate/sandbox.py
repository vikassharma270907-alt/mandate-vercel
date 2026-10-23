"""Airwallex sandbox adapters.

``FakeAirwallex`` is an in-memory simulator used by the demo and tests.  It
enforces the same control payload that would be sent to Airwallex, including
single-use behaviour, merchant/category allowlists and cumulative caps.

``LiveAirwallexAdapter`` is the only code allowed to call ``/api/v1``.  Its
request shapes are UNCONFIRMED until they are compared with the current Kit 2
schema and exercised against the real sandbox (see docs/SANDBOX_RUNBOOK.md).
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from .money import MoneyError, format_money, parse_money, to_payload_number

__all__ = [
    "CardDeclined",
    "CardAdapter",
    "FakeAirwallex",
    "LiveAirwallexAdapter",
    "LiveConfig",
]

CURRENCY = "USD"
MCC = "5734"


class CardDeclined(RuntimeError):
    """Raised only for adapter-level misuse, not network declines."""


class CardAdapter(Protocol):
    def available_balance(self) -> Decimal: ...

    def deposit(self, amount: Decimal | str | int | float, currency: str = CURRENCY) -> dict[str, Any]: ...

    def create_card(
        self, controls: Mapping[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]: ...

    def authorize(
        self,
        card_id: str,
        amount: Decimal | str | int | float,
        *,
        currency: str = CURRENCY,
        mcc: str = MCC,
        merchant: str = "CloudMetrics Inc",
        capture: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]: ...

    def list_transactions(self, card_id: str | None = None) -> list[dict[str, Any]]: ...


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _control_limits(controls: Mapping[str, Any]) -> tuple[Decimal, Decimal, str]:
    transaction_limits = controls.get("transaction_limits")
    if not isinstance(transaction_limits, Mapping):
        raise CardDeclined("card controls are missing transaction_limits")
    limits = transaction_limits.get("limits")
    if not isinstance(limits, list) or not limits:
        raise CardDeclined("card controls must contain non-empty limits")
    per_transaction: Decimal | None = None
    cumulative: Decimal | None = None
    cumulative_interval: str | None = None
    for limit in limits:
        if not isinstance(limit, Mapping):
            raise CardDeclined("each card limit must be an object")
        try:
            amount = parse_money(limit.get("amount"), field="card limit")
        except MoneyError as exc:
            raise CardDeclined(str(exc)) from exc
        interval = str(limit.get("interval", "")).upper()
        if interval == "PER_TRANSACTION":
            per_transaction = amount
        elif interval in ("MONTHLY", "ALL_TIME"):
            cumulative = amount
            cumulative_interval = interval
    if per_transaction is None or cumulative is None or cumulative_interval is None:
        raise CardDeclined(
            "card controls need a PER_TRANSACTION limit and a MONTHLY or ALL_TIME limit"
        )
    return per_transaction, cumulative, cumulative_interval


class FakeAirwallex:
    """In-memory sandbox that enforces card controls exactly."""

    def __init__(
        self,
        balance: Decimal | str | int | float = "0.00",
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._balance = parse_money(balance, field="initial balance")
        self._now_fn = now_fn or _now_utc
        self._cards: dict[str, dict[str, Any]] = {}
        self._transactions: list[dict[str, Any]] = []

    # -- account ---------------------------------------------------------
    def available_balance(self) -> Decimal:
        # Mirrors Airwallex: available_amount, never total_amount.
        return self._balance

    def deposit(
        self,
        amount: Decimal | str | int | float,
        currency: str = CURRENCY,
    ) -> dict[str, Any]:
        if currency.upper() != CURRENCY:
            raise CardDeclined("sandbox supports USD only")
        value = parse_money(amount, field="deposit amount")
        if value <= 0:
            raise CardDeclined("deposit amount must be greater than zero")
        self._balance += value
        return {
            "status": "SUCCEEDED",
            "currency": CURRENCY,
            "amount": format_money(value),
            "available_balance": format_money(self._balance),
            "synthetic": True,
        }

    # -- cards -----------------------------------------------------------
    def create_card(
        self,
        controls: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(controls, Mapping):
            raise CardDeclined("controls must be a mapping")
        if not controls.get("allowed_currencies"):
            raise CardDeclined("allowed_currencies must not be empty")
        if not controls.get("allowed_merchant_categories"):
            raise CardDeclined("allowed_merchant_categories must not be empty")
        if controls.get("allowed_transaction_count") not in ("SINGLE", "MULTIPLE"):
            raise CardDeclined("allowed_transaction_count must be SINGLE or MULTIPLE")
        _control_limits(controls)

        card_id = f"card_{uuid.uuid4().hex[:16]}"
        card = {
            "id": card_id,
            "status": "ACTIVE",
            "controls": json.loads(json.dumps(controls)),
            "created_at": self._now_fn().isoformat(timespec="seconds"),
            "request_id": request_id,
        }
        self._cards[card_id] = card
        return json.loads(json.dumps(card))

    def _sum_in_window(
        self,
        card_id: str,
        interval: str,
        now: datetime,
    ) -> Decimal:
        total = Decimal("0.00")
        for transaction in self._transactions:
            if transaction["card_id"] != card_id:
                continue
            if transaction["status"] != "APPROVED":
                continue
            created = datetime.fromisoformat(transaction["created_at"])
            if interval == "MONTHLY":
                if (created.year, created.month) != (now.year, now.month):
                    continue
            total += parse_money(transaction["amount"])
        return total

    def authorize(
        self,
        card_id: str,
        amount: Decimal | str | int | float,
        *,
        currency: str = CURRENCY,
        mcc: str = MCC,
        merchant: str = "CloudMetrics Inc",
        capture: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or self._now_fn()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        try:
            value = parse_money(amount, field="authorization amount")
        except MoneyError as exc:
            raise CardDeclined(str(exc)) from exc
        if value <= 0:
            raise CardDeclined("authorization amount must be greater than zero")

        decline_reason: str | None = None
        card = self._cards.get(card_id)
        if card is None:
            decline_reason = "unknown card"
        controls = card["controls"] if card else {}

        if decline_reason is None:
            currency = str(currency).strip().upper()
            mcc = str(mcc).strip()
            allowed_currencies = [
                str(item).upper() for item in controls.get("allowed_currencies", [])
            ]
            allowed_mccs = [str(item) for item in controls.get("allowed_merchant_categories", [])]
            if currency not in allowed_currencies:
                decline_reason = f"currency {currency} is not allowed on this card"
            elif mcc not in allowed_mccs:
                decline_reason = f"MCC {mcc} is not allowed on this card"

        if decline_reason is None:
            per_transaction, cumulative, cumulative_interval = _control_limits(controls)
            if value > per_transaction:
                decline_reason = (
                    f"amount {format_money(value)} exceeds the per-transaction limit "
                    f"{format_money(per_transaction)}"
                )
            elif controls.get("allowed_transaction_count") == "SINGLE":
                used = sum(
                    1
                    for item in self._transactions
                    if item["card_id"] == card_id and item["status"] == "APPROVED"
                )
                if used >= 1:
                    decline_reason = "single-use card has already completed a transaction"
            if decline_reason is None:
                already = self._sum_in_window(card_id, cumulative_interval, current_time)
                if already + value > cumulative:
                    decline_reason = (
                        f"amount {format_money(value)} would exceed the {cumulative_interval} "
                        f"cumulative limit {format_money(cumulative)} "
                        f"(already approved {format_money(already)})"
                    )
            if decline_reason is None and self._balance < value:
                decline_reason = (
                    f"insufficient available balance {format_money(self._balance)} for "
                    f"{format_money(value)}"
                )

        status = "DECLINED" if decline_reason else "APPROVED"
        transaction = {
            "id": f"txn_{uuid.uuid4().hex[:16]}",
            "card_id": card_id,
            "amount": format_money(value),
            "currency": str(currency).upper(),
            "merchant_category": str(mcc),
            "merchant": str(merchant),
            "status": status,
            "decline_reason": decline_reason,
            "captured": bool(capture and status == "APPROVED"),
            "created_at": current_time.isoformat(timespec="seconds"),
            "synthetic": True,
        }
        self._transactions.append(transaction)
        if transaction["captured"]:
            self._balance -= value
        return json.loads(json.dumps(transaction))

    def list_transactions(self, card_id: str | None = None) -> list[dict[str, Any]]:
        if card_id is None:
            return json.loads(json.dumps(self._transactions))
        return json.loads(
            json.dumps(
                [item for item in self._transactions if item["card_id"] == card_id]
            )
        )


# ---------------------------------------------------------------------------
# Live adapter (UNCONFIRMED request shapes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveConfig:
    base_url: str
    api_key: str
    api_version: str = "2022-11-11"
    card_program_id: str | None = None
    timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "LiveConfig":
        base_url = os.environ.get("AIRWALLEX_BASE_URL", "https://api-demo.airwallex.com")
        api_key = os.environ.get("AIRWALLEX_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "AIRWALLEX_API_KEY is not set. Use the fake sandbox for the offline demo."
            )
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            api_version=os.environ.get("AIRWALLEX_API_VERSION", "2022-11-11"),
            card_program_id=os.environ.get("AIRWALLEX_CARD_PROGRAM_ID") or None,
            timeout=float(os.environ.get("AIRWALLEX_TIMEOUT", "20")),
        )


class LiveAirwallexAdapter:
    """Thin live sandbox client.  Every endpoint shape is UNCONFIRMED.

    Compare requests with the current Airwallex Kit 2 schema before trusting a
    live run.  This adapter never falls back to fake behaviour silently.
    """

    def __init__(self, config: LiveConfig | None = None) -> None:
        self.config = config or LiveConfig.from_env()
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    # -- HTTP ------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        authenticated: bool = True,
    ) -> Any:
        if authenticated:
            self.authenticate()
        url = f"{self.config.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-version": self.config.api_version,
        }
        if authenticated and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Airwallex {method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Airwallex {method} {path} failed: {exc.reason}") from exc
        if not raw:
            return {}
        return json.loads(raw)

    def authenticate(self) -> str:
        if self._token and self._token_expires_at:
            if datetime.now(timezone.utc) < self._token_expires_at:
                return self._token
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "x-api-version": self.config.api_version,
        }
        request = urllib.request.Request(
            f"{self.config.base_url}/api/v1/authentication/login",
            data=b"{}",
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Airwallex login failed with HTTP {exc.code}: {detail}"
            ) from exc
        token = (
            payload.get("token")
            or payload.get("data", {}).get("token")
            or payload.get("access_token")
        )
        if not token:
            raise RuntimeError(f"Airwallex login response had no token: {payload!r}")
        self._token = str(token)
        # Sandbox tokens are short-lived; refresh every 20 minutes unless the
        # response supplied a usable expiry.
        self._token_expires_at = datetime.now(timezone.utc).replace(microsecond=0)
        self._token_expires_at = self._token_expires_at.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + 1200, tz=timezone.utc
        )
        return self._token

    # -- account ---------------------------------------------------------
    def available_balance(self) -> Decimal:
        payload = self._request(
            "GET",
            "/api/v1/balances/current",
            query={"currency": CURRENCY},
        )
        data = payload.get("data", payload)
        candidate: Any = None
        if isinstance(data, list):
            for item in data:
                if str(item.get("currency", "")).upper() == CURRENCY:
                    candidate = item
                    break
            if candidate is None and data:
                candidate = data[0]
        elif isinstance(data, Mapping):
            candidate = data
        if not isinstance(candidate, Mapping):
            raise RuntimeError(f"could not find USD balance in response: {payload!r}")
        available = candidate.get("available_amount", candidate.get("available"))
        if available is None:
            raise RuntimeError(f"balance response had no available_amount: {payload!r}")
        try:
            return parse_money(str(available), field="available_amount")
        except MoneyError as exc:
            raise RuntimeError(str(exc)) from exc

    def deposit(
        self,
        amount: Decimal | str | int | float,
        currency: str = CURRENCY,
    ) -> dict[str, Any]:
        value = parse_money(amount, field="deposit amount")
        return self._request(
            "POST",
            "/api/v1/simulation/deposit/create",
            body={"amount": to_payload_number(value), "currency": currency.upper()},
        )

    # -- cards -----------------------------------------------------------
    def create_card(
        self,
        controls: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = dict(controls)
        body["request_id"] = request_id or f"req_{uuid.uuid4().hex}"
        if self.config.card_program_id:
            body["card_program_id"] = self.config.card_program_id
        return self._request("POST", "/api/v1/issuing/cards/create", body=body)

    def authorize(
        self,
        card_id: str,
        amount: Decimal | str | int | float,
        *,
        currency: str = CURRENCY,
        mcc: str = MCC,
        merchant: str = "CloudMetrics Inc",
        capture: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        del capture, now  # sandbox simulation endpoint controls its own lifecycle
        value = parse_money(amount, field="authorization amount")
        return self._request(
            "POST",
            "/api/v1/simulation/issuing/create",
            body={
                "card_id": card_id,
                "amount": to_payload_number(value),
                "currency": currency.upper(),
                "merchant_category": mcc,
                "merchant_name": merchant,
            },
        )

    def list_transactions(self, card_id: str | None = None) -> list[dict[str, Any]]:
        query = {"card_id": card_id} if card_id else None
        payload = self._request("GET", "/api/v1/issuing/transactions", query=query)
        data = payload.get("data", payload)
        if isinstance(data, Mapping):
            data = data.get("items", [])
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected transactions response: {payload!r}")
        return data
