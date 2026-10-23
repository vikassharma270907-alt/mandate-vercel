"""Decision Passport creation and validation.

A passport binds the seven fields a human actually approved with an
HMAC-SHA256 ``intent_hash``.  A second keyed MAC, ``record_seal``, covers the
stored record (approval id, status, expiry and the hash itself) so those fields
cannot be altered in storage.

Status precedence is INVALID, then MISMATCH, then EXPIRED, then VALID.
"""

from __future__ import annotations

import hmac
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping

from .money import MoneyError, format_money, parse_money

__all__ = [
    "APPROVED_FIELDS",
    "PASSPORT_FIELDS",
    "STATUS_APPROVED",
    "STATUS_INVALID",
    "STATUS_MISMATCH",
    "STATUS_EXPIRED",
    "STATUS_VALID",
    "PassportError",
    "PassportKeyError",
    "InvalidIntent",
    "ApprovedIntent",
    "Passport",
    "PassportValidation",
    "compute_intent_hash",
    "issue_passport",
    "validate_passport",
    "passport_to_dict",
    "intent_to_dict",
    "coerce_intent",
]

STATUS_APPROVED = "APPROVED"
STATUS_INVALID = "INVALID"
STATUS_MISMATCH = "MISMATCH"
STATUS_EXPIRED = "EXPIRED"
STATUS_VALID = "VALID"

APPROVED_FIELDS = (
    "vendor",
    "amount",
    "currency",
    "billing_cadence",
    "merchant_category",
    "evidence_reference",
    "policy_version",
)

PASSPORT_FIELDS = (
    "approval_id",
    "vendor",
    "amount",
    "currency",
    "billing_cadence",
    "merchant_category",
    "evidence_reference",
    "policy_version",
    "intent_hash",
    "approval_expiry",
    "approval_status",
    "record_seal",
)

_MCC_RE = re.compile(r"[0-9]{4}\Z")
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")
MIN_KEY_BYTES = 32


class PassportError(ValueError):
    """Base class for passport failures."""


class PassportKeyError(PassportError):
    """The signing key is missing or too short."""


class InvalidIntent(PassportError):
    """An approved-intent field is missing or malformed."""


def _clean(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidIntent(f"{field_name} must be a string")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if not cleaned:
        raise InvalidIntent(f"{field_name} must not be empty")
    return cleaned


def _key_bytes(key: bytes | str) -> bytes:
    if isinstance(key, str):
        key_bytes = key.encode("utf-8")
    elif isinstance(key, (bytes, bytearray)):
        key_bytes = bytes(key)
    else:
        raise PassportKeyError("passport key must be bytes or str")
    if len(key_bytes) < MIN_KEY_BYTES:
        raise PassportKeyError(
            f"passport key must be at least {MIN_KEY_BYTES} bytes for HMAC-SHA256"
        )
    return key_bytes


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PassportError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ApprovedIntent:
    """The seven fields a human approves.  Normalised on construction."""

    vendor: str
    amount: Decimal
    currency: str
    billing_cadence: str
    merchant_category: str
    evidence_reference: str
    policy_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor", _clean(self.vendor, field_name="vendor"))
        try:
            amount = parse_money(self.amount, field="amount")
        except MoneyError as exc:
            raise InvalidIntent(str(exc)) from exc
        if amount <= 0:
            raise InvalidIntent("amount must be greater than zero")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(
            self,
            "currency",
            _clean(self.currency, field_name="currency").upper(),
        )
        if not _CURRENCY_RE.fullmatch(self.currency):
            raise InvalidIntent("currency must be a three-letter ISO code")
        object.__setattr__(
            self,
            "billing_cadence",
            _clean(self.billing_cadence, field_name="billing_cadence").upper(),
        )
        if self.billing_cadence not in ("MONTHLY", "ANNUAL"):
            raise InvalidIntent("billing_cadence must be exactly MONTHLY or ANNUAL")
        if not isinstance(self.merchant_category, str):
            raise InvalidIntent("merchant_category must be a string")
        # Check the raw characters before NFKC so full-width digits cannot
        # masquerade as an ASCII MCC.
        mcc = self.merchant_category.strip()
        if not _MCC_RE.fullmatch(mcc):
            raise InvalidIntent("merchant_category must be exactly four ASCII digits")
        object.__setattr__(self, "merchant_category", mcc)
        object.__setattr__(
            self,
            "evidence_reference",
            _clean(self.evidence_reference, field_name="evidence_reference"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _clean(self.policy_version, field_name="policy_version"),
        )


@dataclass(frozen=True)
class Passport:
    approval_id: str
    vendor: str
    amount: Decimal
    currency: str
    billing_cadence: str
    merchant_category: str
    evidence_reference: str
    policy_version: str
    intent_hash: str
    approval_expiry: str
    approval_status: str
    record_seal: str


@dataclass(frozen=True)
class PassportValidation:
    status: str
    reason: str
    passport: Passport | None = None

    @property
    def is_valid(self) -> bool:
        return self.status == STATUS_VALID


def _intent_payload(intent: ApprovedIntent) -> bytes:
    payload = {
        "amount": format_money(intent.amount),
        "billing_cadence": intent.billing_cadence,
        "currency": intent.currency,
        "evidence_reference": intent.evidence_reference,
        "merchant_category": intent.merchant_category,
        "policy_version": intent.policy_version,
        "vendor": intent.vendor,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def compute_intent_hash(intent: ApprovedIntent, key: bytes | str) -> str:
    if not isinstance(intent, ApprovedIntent):
        raise InvalidIntent("intent must be an ApprovedIntent")
    key_bytes = _key_bytes(key)
    return hmac.new(key_bytes, _intent_payload(intent), sha256).hexdigest()


def _passport_record(passport: Passport) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for field_name in PASSPORT_FIELDS:
        if field_name == "record_seal":
            continue
        value = getattr(passport, field_name)
        if field_name == "amount":
            record[field_name] = format_money(value)
        else:
            record[field_name] = value
    return record


def _record_payload(passport: Passport) -> bytes:
    return json.dumps(
        _passport_record(passport),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _seal(passport: Passport, key: bytes | str) -> str:
    return hmac.new(_key_bytes(key), _record_payload(passport), sha256).hexdigest()


def issue_passport(
    intent: ApprovedIntent,
    key: bytes | str,
    *,
    approval_id: str | None = None,
    expires_in: timedelta = timedelta(minutes=15),
    now: datetime | None = None,
) -> Passport:
    """Issue a sealed passport for exactly these approved terms."""

    if not isinstance(intent, ApprovedIntent):
        raise InvalidIntent("intent must be an ApprovedIntent")
    key_bytes = _key_bytes(key)

    issued_at = _as_utc(now or _utc_now(), field_name="now")
    if not isinstance(expires_in, timedelta):
        raise PassportError("expires_in must be a timedelta")
    if expires_in <= timedelta(0):
        raise PassportError("expires_in must be positive")
    expiry = issued_at + expires_in

    if approval_id is None:
        approval_id = f"apr_{uuid.uuid4().hex}"
    elif not isinstance(approval_id, str) or not approval_id.strip():
        raise PassportError("approval_id must be a non-empty string")
    else:
        approval_id = approval_id.strip()

    intent_hash = compute_intent_hash(intent, key_bytes)
    passport = Passport(
        approval_id=approval_id,
        vendor=intent.vendor,
        amount=intent.amount,
        currency=intent.currency,
        billing_cadence=intent.billing_cadence,
        merchant_category=intent.merchant_category,
        evidence_reference=intent.evidence_reference,
        policy_version=intent.policy_version,
        intent_hash=intent_hash,
        approval_expiry=expiry.isoformat(timespec="seconds"),
        approval_status=STATUS_APPROVED,
        record_seal="",
    )
    return Passport(
        **{**passport.__dict__, "record_seal": _seal(passport, key_bytes)}
    )


def _intent_from_passport(passport: Passport) -> ApprovedIntent:
    return ApprovedIntent(
        vendor=passport.vendor,
        amount=passport.amount,
        currency=passport.currency,
        billing_cadence=passport.billing_cadence,
        merchant_category=passport.merchant_category,
        evidence_reference=passport.evidence_reference,
        policy_version=passport.policy_version,
    )


def coerce_intent(value: ApprovedIntent | Mapping[str, Any]) -> ApprovedIntent:
    if isinstance(value, ApprovedIntent):
        return value
    if isinstance(value, Mapping):
        missing = [field_name for field_name in APPROVED_FIELDS if field_name not in value]
        if missing:
            raise InvalidIntent(f"intent is missing fields: {', '.join(missing)}")
        return ApprovedIntent(**{field_name: value[field_name] for field_name in APPROVED_FIELDS})
    raise InvalidIntent("expected_intent must be an ApprovedIntent or mapping")


def validate_passport(
    passport: Passport,
    key: bytes | str,
    *,
    now: datetime | None = None,
    expected_intent: ApprovedIntent | Mapping[str, Any] | None = None,
) -> PassportValidation:
    """Validate a passport in precedence order: INVALID, MISMATCH, EXPIRED, VALID."""

    key_bytes = _key_bytes(key)
    current_time = _as_utc(now or _utc_now(), field_name="now")

    # Structural checks are all INVALID: a record that cannot be parsed carries
    # no trustworthy approval at all.
    if not isinstance(passport, Passport):
        return PassportValidation(STATUS_INVALID, "not a passport record")
    if passport.approval_status != STATUS_APPROVED:
        return PassportValidation(
            STATUS_INVALID,
            f"approval_status is {passport.approval_status!r}, not APPROVED",
        )
    if not isinstance(passport.approval_id, str) or not passport.approval_id.strip():
        return PassportValidation(STATUS_INVALID, "approval_id is empty")
    if not isinstance(passport.intent_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", passport.intent_hash
    ):
        return PassportValidation(STATUS_INVALID, "intent_hash is malformed")
    if not isinstance(passport.record_seal, str) or not re.fullmatch(
        r"[0-9a-f]{64}", passport.record_seal
    ):
        return PassportValidation(STATUS_INVALID, "record_seal is malformed")

    try:
        expiry = datetime.fromisoformat(passport.approval_expiry)
    except (TypeError, ValueError):
        return PassportValidation(STATUS_INVALID, "approval_expiry is not a valid timestamp")
    if expiry.tzinfo is None:
        return PassportValidation(STATUS_INVALID, "approval_expiry must include a timezone")

    try:
        stored_intent = _intent_from_passport(passport)
        recomputed_hash = compute_intent_hash(stored_intent, key_bytes)
    except (InvalidIntent, MoneyError) as exc:
        return PassportValidation(STATUS_INVALID, f"stored terms are invalid: {exc}")

    if not hmac.compare_digest(recomputed_hash, passport.intent_hash):
        return PassportValidation(
            STATUS_INVALID,
            "stored terms do not match the sealed intent_hash",
        )

    if not hmac.compare_digest(_seal(passport, key_bytes), passport.record_seal):
        return PassportValidation(
            STATUS_INVALID,
            "record_seal does not match the stored record",
        )

    # MISMATCH takes precedence over EXPIRED by design.
    if expected_intent is not None:
        try:
            expected = coerce_intent(expected_intent)
        except PassportError as exc:
            return PassportValidation(STATUS_MISMATCH, f"expected intent is invalid: {exc}")
        expected_hash = compute_intent_hash(expected, key_bytes)
        if not hmac.compare_digest(expected_hash, passport.intent_hash):
            return PassportValidation(
                STATUS_MISMATCH,
                "passport does not match the submitted intent",
            )

    if current_time >= expiry:
        return PassportValidation(
            STATUS_EXPIRED,
            f"passport expired at {passport.approval_expiry}",
        )

    return PassportValidation(STATUS_VALID, "passport is valid", passport)


def intent_to_dict(intent: ApprovedIntent) -> dict[str, Any]:
    return {
        "vendor": intent.vendor,
        "amount": format_money(intent.amount),
        "currency": intent.currency,
        "billing_cadence": intent.billing_cadence,
        "merchant_category": intent.merchant_category,
        "evidence_reference": intent.evidence_reference,
        "policy_version": intent.policy_version,
    }


def passport_to_dict(passport: Passport) -> dict[str, Any]:
    if not isinstance(passport, Passport):
        raise PassportError("passport must be a Passport")
    return {
        "approval_id": passport.approval_id,
        "vendor": passport.vendor,
        "amount": format_money(passport.amount),
        "currency": passport.currency,
        "billing_cadence": passport.billing_cadence,
        "merchant_category": passport.merchant_category,
        "evidence_reference": passport.evidence_reference,
        "policy_version": passport.policy_version,
        "intent_hash": passport.intent_hash,
        "approval_expiry": passport.approval_expiry,
        "approval_status": passport.approval_status,
        "record_seal": passport.record_seal,
    }
