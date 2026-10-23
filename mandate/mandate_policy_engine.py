"""Mandate deterministic policy engine.

Side-effect free: this module never performs I/O, reads the clock or calls a
network.  Every number that can move money is computed here, not by a language
model.

The engine models an inclusive weekly horizon (weeks 0..N, default 0..8).
Outflows are applied before inflows within a week and the minimum is checked
before inflows land.  The conservative case drops expected inflows but always
counts expected outflows.  A balance exactly at the reserve floor is safe.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .money import MoneyError, format_money, parse_money

__all__ = [
    "POLICY_VERSION",
    "PolicyError",
    "FlowValidationError",
    "FlowOutsideHorizon",
    "InvalidTerms",
    "FlowKind",
    "Flow",
    "CashContext",
    "PurchaseTerms",
    "Payment",
    "PlanForecast",
    "PolicyEvaluation",
    "build_monthly_payments",
    "build_annual_payments",
    "forecast_plan",
    "evaluate_policy",
    "evaluate_live_plan",
    "policy_output",
]

POLICY_VERSION = "mandate-policy/2.2"

DECISION_ANNUAL = "PROPOSE_ANNUAL"
DECISION_MONTHLY = "PROPOSE_MONTHLY"
DECISION_DEFER = "DEFER"
DECISIONS = (DECISION_ANNUAL, DECISION_MONTHLY, DECISION_DEFER)

LIVE_HOLD = "HOLD"
LIVE_FREEZE = "RECOMMEND_FREEZE_AND_ESCALATE"

_MCC_RE = re.compile(r"[0-9]{4}\Z")
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")


class PolicyError(ValueError):
    """Base class for deterministic policy errors."""


class FlowValidationError(PolicyError):
    """A cash flow has an impossible sign, week or amount."""


class FlowOutsideHorizon(PolicyError):
    """A flow would occur outside the modelled horizon."""


class InvalidTerms(PolicyError):
    """Purchase terms cannot be evaluated safely."""


def _clean_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidTerms(f"{field_name} must be a string")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if not cleaned:
        raise InvalidTerms(f"{field_name} must not be empty")
    return cleaned


def _require_week(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{field_name} must be an integer week")
    if value < 0:
        raise PolicyError(f"{field_name} must be zero or greater")
    return value


class FlowKind(str, Enum):
    CONFIRMED_INFLOW = "CONFIRMED_INFLOW"
    EXPECTED_INFLOW = "EXPECTED_INFLOW"
    COMMITTED_OUTFLOW = "COMMITTED_OUTFLOW"
    EXPECTED_OUTFLOW = "EXPECTED_OUTFLOW"

    @property
    def is_inflow(self) -> bool:
        return self in (FlowKind.CONFIRMED_INFLOW, FlowKind.EXPECTED_INFLOW)

    @property
    def is_expected(self) -> bool:
        return self in (FlowKind.EXPECTED_INFLOW, FlowKind.EXPECTED_OUTFLOW)


@dataclass(frozen=True)
class Flow:
    """A trusted cash flow in major units.

    Amounts are positive; direction is carried by ``kind``.  Signs are
    validated so an inflow cannot accidentally be entered as a negative.
    """

    kind: FlowKind
    amount: Decimal
    week: int
    label: str = ""

    def __post_init__(self) -> None:
        try:
            kind = FlowKind(self.kind)
        except ValueError as exc:
            raise FlowValidationError(f"unknown flow kind: {self.kind!r}") from exc
        object.__setattr__(self, "kind", kind)

        try:
            amount = parse_money(self.amount, field="flow amount")
        except MoneyError as exc:
            raise FlowValidationError(str(exc)) from exc
        if amount <= 0:
            raise FlowValidationError("flow amount must be greater than zero")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "week", _require_week(self.week, field_name="flow week"))
        if not isinstance(self.label, str):
            raise FlowValidationError("flow label must be a string")


@dataclass(frozen=True)
class CashContext:
    """Trusted cash data used by the engine.

    ``available_balance`` must be the wallet's ``available_amount`` (not
    ``total_amount``).  Callers are responsible for sourcing it from the bank
    adapter, never from an untrusted document.
    """

    available_balance: Decimal
    reserve_floor: Decimal
    flows: tuple[Flow, ...] = field(default_factory=tuple)
    horizon_weeks: int = 8
    currency: str = "USD"

    def __post_init__(self) -> None:
        try:
            balance = parse_money(self.available_balance, field="available_balance")
            floor = parse_money(self.reserve_floor, field="reserve_floor")
        except MoneyError as exc:
            raise FlowValidationError(str(exc)) from exc
        object.__setattr__(self, "available_balance", balance)
        object.__setattr__(self, "reserve_floor", floor)

        horizon = _require_week(self.horizon_weeks, field_name="horizon_weeks")
        object.__setattr__(self, "horizon_weeks", horizon)

        if not isinstance(self.currency, str):
            raise FlowValidationError("currency must be a string")
        currency = self.currency.strip().upper()
        if not _CURRENCY_RE.fullmatch(currency):
            raise FlowValidationError("currency must be a three-letter ISO code")
        object.__setattr__(self, "currency", currency)

        flows = tuple(self.flows or ())
        for flow in flows:
            if not isinstance(flow, Flow):
                raise FlowValidationError("flows must contain Flow instances")
            if flow.week > horizon:
                raise FlowOutsideHorizon(
                    f"flow at week {flow.week} is outside the inclusive "
                    f"week 0..{horizon} horizon"
                )
        object.__setattr__(self, "flows", flows)


@dataclass(frozen=True)
class PurchaseTerms:
    """The terms being evaluated.  Prices are supplied by the caller after
    document extraction; the human approves the exact terms before a passport
    is issued."""

    vendor: str
    monthly_price: Decimal
    annual_price: Decimal
    currency: str = "USD"
    merchant_category: str = "5734"
    monthly_first_week: int = 0
    annual_payment_week: int = 0
    monthly_cadence_weeks: int = 4

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor", _clean_text(self.vendor, field_name="vendor"))

        try:
            monthly = parse_money(self.monthly_price, field="monthly_price")
            annual = parse_money(self.annual_price, field="annual_price")
        except MoneyError as exc:
            raise InvalidTerms(str(exc)) from exc
        if monthly <= 0 or annual <= 0:
            raise InvalidTerms("prices must be greater than zero")
        object.__setattr__(self, "monthly_price", monthly)
        object.__setattr__(self, "annual_price", annual)

        if not isinstance(self.currency, str):
            raise InvalidTerms("currency must be a string")
        currency = self.currency.strip().upper()
        if not _CURRENCY_RE.fullmatch(currency):
            raise InvalidTerms("currency must be a three-letter ISO code")
        object.__setattr__(self, "currency", currency)

        if not isinstance(self.merchant_category, str):
            raise InvalidTerms("merchant_category must be a string")
        mcc = self.merchant_category.strip()
        if not _MCC_RE.fullmatch(mcc):
            raise InvalidTerms("merchant_category must be exactly four ASCII digits")
        object.__setattr__(self, "merchant_category", mcc)

        object.__setattr__(
            self,
            "monthly_first_week",
            _require_week(self.monthly_first_week, field_name="monthly_first_week"),
        )
        object.__setattr__(
            self,
            "annual_payment_week",
            _require_week(self.annual_payment_week, field_name="annual_payment_week"),
        )
        cadence = _require_week(self.monthly_cadence_weeks, field_name="monthly_cadence_weeks")
        if cadence < 1:
            raise InvalidTerms("monthly_cadence_weeks must be at least one")
        object.__setattr__(self, "monthly_cadence_weeks", cadence)


@dataclass(frozen=True)
class Payment:
    week: int
    amount: Decimal


@dataclass(frozen=True)
class PlanForecast:
    plan: str
    payments: tuple[Payment, ...]
    confirmed_minimum_balance: Decimal
    confirmed_minimum_week: int
    confirmed_ending_balance: Decimal
    with_expected_minimum_balance: Decimal
    with_expected_minimum_week: int
    with_expected_ending_balance: Decimal
    confirmed_trajectory: tuple[tuple[int, Decimal], ...]
    with_expected_trajectory: tuple[tuple[int, Decimal], ...]
    reserve_floor: Decimal

    @property
    def cash_safe(self) -> bool:
        return self.confirmed_minimum_balance >= self.reserve_floor

    @property
    def shortfall(self) -> Decimal:
        return max(Decimal("0.00"), self.reserve_floor - self.confirmed_minimum_balance)


@dataclass(frozen=True)
class PolicyEvaluation:
    decision: str
    reason: str
    terms: PurchaseTerms
    annual: PlanForecast
    monthly: PlanForecast
    annual_saving: Decimal
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class _Leg:
    minimum: Decimal
    minimum_week: int
    ending: Decimal
    trajectory: tuple[tuple[int, Decimal], ...]


def build_monthly_payments(
    terms: PurchaseTerms, horizon_weeks: int, *, include_expected: bool = False
) -> tuple[Payment, ...]:
    """Build the monthly schedule on a 4-week cadence.

    Weeks are ``monthly_first_week``, ``+4``, ``+8`` ... while inside the
    inclusive horizon.  A first payment outside the horizon is an error, never
    silently skipped.
    """

    del include_expected  # schedule shape does not depend on cash treatment
    if terms.monthly_first_week > horizon_weeks:
        raise FlowOutsideHorizon(
            f"monthly purchase at week {terms.monthly_first_week} is outside the "
            f"inclusive week 0..{horizon_weeks} horizon"
        )
    payments = tuple(
        Payment(week=week, amount=terms.monthly_price)
        for week in range(
            terms.monthly_first_week,
            horizon_weeks + 1,
            terms.monthly_cadence_weeks,
        )
    )
    if not payments:
        raise FlowOutsideHorizon("monthly schedule contains no payment inside the horizon")
    return payments


def build_annual_payments(terms: PurchaseTerms, horizon_weeks: int) -> tuple[Payment, ...]:
    if terms.annual_payment_week > horizon_weeks:
        raise FlowOutsideHorizon(
            f"annual purchase at week {terms.annual_payment_week} is outside the "
            f"inclusive week 0..{horizon_weeks} horizon"
        )
    return (Payment(week=terms.annual_payment_week, amount=terms.annual_price),)


def _run_leg(
    context: CashContext,
    payments: Sequence[Payment],
    *,
    include_expected_inflows: bool,
) -> _Leg:
    payments_by_week: dict[int, Decimal] = {}
    for payment in payments:
        if payment.week < 0 or payment.week > context.horizon_weeks:
            raise FlowOutsideHorizon(
                f"purchase flow at week {payment.week} is outside the inclusive "
                f"week 0..{context.horizon_weeks} horizon"
            )
        payments_by_week[payment.week] = (
            payments_by_week.get(payment.week, Decimal("0.00")) + payment.amount
        )

    outflows: dict[int, Decimal] = {}
    inflows: dict[int, Decimal] = {}
    for flow in context.flows:
        if flow.kind.is_inflow:
            if flow.kind is FlowKind.EXPECTED_INFLOW and not include_expected_inflows:
                continue
            inflows[flow.week] = inflows.get(flow.week, Decimal("0.00")) + flow.amount
        else:
            # Committed and expected outflows always count.
            outflows[flow.week] = outflows.get(flow.week, Decimal("0.00")) + flow.amount

    balance = context.available_balance
    minimum = balance
    minimum_week = 0
    trajectory: list[tuple[int, Decimal]] = []

    for week in range(context.horizon_weeks + 1):
        week_out = outflows.get(week, Decimal("0.00")) + payments_by_week.get(
            week, Decimal("0.00")
        )
        balance -= week_out
        # Outflow-first: the minimum is checked before this week's inflows land.
        if balance < minimum:
            minimum = balance
            minimum_week = week
        balance += inflows.get(week, Decimal("0.00"))
        trajectory.append((week, balance))

    return _Leg(
        minimum=minimum,
        minimum_week=minimum_week,
        ending=balance,
        trajectory=tuple(trajectory),
    )


def forecast_plan(
    context: CashContext,
    payments: Sequence[Payment],
    *,
    plan: str,
) -> PlanForecast:
    """Forecast one purchase plan in both cash treatments."""

    if not isinstance(context, CashContext):
        raise PolicyError("context must be a CashContext")
    confirmed = _run_leg(context, payments, include_expected_inflows=False)
    inclusive = _run_leg(context, payments, include_expected_inflows=True)
    return PlanForecast(
        plan=plan,
        payments=tuple(payments),
        confirmed_minimum_balance=confirmed.minimum,
        confirmed_minimum_week=confirmed.minimum_week,
        confirmed_ending_balance=confirmed.ending,
        with_expected_minimum_balance=inclusive.minimum,
        with_expected_minimum_week=inclusive.minimum_week,
        with_expected_ending_balance=inclusive.ending,
        confirmed_trajectory=confirmed.trajectory,
        with_expected_trajectory=inclusive.trajectory,
        reserve_floor=context.reserve_floor,
    )


def _join_numbers(values: Iterable[int]) -> str:
    numbers = [str(value) for value in values]
    if not numbers:
        return ""
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return f"{numbers[0]} and {numbers[1]}"
    return f"{', '.join(numbers[:-1])}, and {numbers[-1]}"


def _schedule_phrase(payments: Sequence[Payment], currency: str) -> str:
    amounts = {payment.amount for payment in payments}
    weeks = _join_numbers(payment.week for payment in payments)
    if len(amounts) == 1:
        amount = format_money(payments[0].amount)
        noun = "payment" if len(payments) == 1 else "payments"
        return f"{len(payments)} {noun} of {amount} {currency} at week{'s' if len(payments) != 1 else ''} {weeks}"
    parts = ", ".join(
        f"{format_money(payment.amount)} {currency} at week {payment.week}"
        for payment in payments
    )
    return f"payments of {parts}"


def _plan_sentence(
    label: str,
    forecast: PlanForecast,
    terms: PurchaseTerms,
    context: CashContext,
) -> str:
    currency = context.currency
    floor = format_money(context.reserve_floor)
    minimum = format_money(forecast.confirmed_minimum_balance)
    week = forecast.confirmed_minimum_week
    if label == "Annual":
        schedule = (
            f"Annual billing ({format_money(terms.annual_price)} {currency} at "
            f"week {terms.annual_payment_week})"
        )
    else:
        schedule = f"Monthly billing ({_schedule_phrase(forecast.payments, currency)})"

    if forecast.cash_safe:
        return (
            f"{schedule} keeps the confirmed-cash minimum at {minimum} {currency} "
            f"at week {week}, at or above the {floor} {currency} reserve floor."
        )
    return (
        f"{schedule} drops the confirmed-cash balance to a minimum of {minimum} "
        f"{currency} at week {week}, breaching the {floor} {currency} reserve floor "
        f"by {format_money(forecast.shortfall)} {currency}."
    )


def evaluate_policy(context: CashContext, terms: PurchaseTerms) -> PolicyEvaluation:
    """Run the deterministic decision table.

    1. Annual is cash-safe and the annual saving is greater than zero -> annual.
    2. Otherwise monthly is cash-safe -> monthly.
    3. Otherwise -> defer.
    """

    if not isinstance(context, CashContext):
        raise PolicyError("context must be a CashContext")
    if not isinstance(terms, PurchaseTerms):
        raise InvalidTerms("terms must be PurchaseTerms")
    if terms.currency != context.currency:
        raise InvalidTerms("terms and context currencies must match")

    monthly = forecast_plan(
        context,
        build_monthly_payments(terms, context.horizon_weeks),
        plan="MONTHLY",
    )
    annual = forecast_plan(
        context,
        build_annual_payments(terms, context.horizon_weeks),
        plan="ANNUAL",
    )
    annual_saving = terms.monthly_price * 12 - terms.annual_price

    if annual.cash_safe and annual_saving > 0:
        decision = DECISION_ANNUAL
    elif monthly.cash_safe:
        decision = DECISION_MONTHLY
    else:
        decision = DECISION_DEFER

    annual_sentence = _plan_sentence("Annual", annual, terms, context)
    monthly_sentence = _plan_sentence("Monthly", monthly, terms, context)
    currency = context.currency
    twelve_months = format_money(terms.monthly_price * 12)

    if annual_saving > 0:
        saving_sentence = (
            f"Annual billing saves {format_money(annual_saving)} {currency} over "
            f"twelve 4-week monthly cycles ({twelve_months} {currency})."
        )
    elif annual_saving == 0:
        saving_sentence = (
            "Annual billing is priced the same as twelve 4-week monthly cycles "
            f"({twelve_months} {currency})."
        )
    else:
        saving_sentence = (
            f"Annual billing costs {format_money(-annual_saving)} {currency} more "
            f"than twelve 4-week monthly cycles ({twelve_months} {currency})."
        )

    if decision == DECISION_ANNUAL:
        decision_sentence = (
            "Both plans are cash-safe and annual billing is cheaper, so the engine "
            "selects PROPOSE_ANNUAL."
        )
    elif decision == DECISION_MONTHLY:
        if annual.cash_safe:
            decision_sentence = (
                "Annual billing is cash-safe but not cheaper, so the engine selects "
                "PROPOSE_MONTHLY."
            )
        else:
            decision_sentence = (
                "Annual billing is not cash-safe, so the engine selects PROPOSE_MONTHLY."
            )
    else:
        decision_sentence = (
            "Neither plan keeps confirmed cash at or above the reserve floor, so the "
            "engine selects DEFER."
        )

    if (
        decision == DECISION_MONTHLY
        and not annual.cash_safe
        and annual.with_expected_minimum_balance >= context.reserve_floor
    ):
        decision_sentence += (
            " Expected inflows do not prevent the annual-plan breach because the "
            f"charge at week {terms.annual_payment_week} lands first."
        )

    reason = " ".join([annual_sentence, monthly_sentence, saving_sentence, decision_sentence])

    return PolicyEvaluation(
        decision=decision,
        reason=reason,
        terms=terms,
        annual=annual,
        monthly=monthly,
        annual_saving=annual_saving,
    )


def evaluate_live_plan(
    context: CashContext,
    terms: PurchaseTerms,
    remaining_payments: int,
    *,
    plan: str = "MONTHLY",
) -> dict[str, Any]:
    """Re-check an already-approved plan against current confirmed cash.

    Recommends only: ``HOLD`` or ``RECOMMEND_FREEZE_AND_ESCALATE``.  It never
    freezes a card or moves money itself.
    """

    if isinstance(remaining_payments, bool) or not isinstance(remaining_payments, int):
        raise PolicyError("remaining_payments must be an integer")
    if remaining_payments < 0:
        raise PolicyError("remaining_payments must be zero or greater")

    normalized_plan = str(plan).strip().upper()
    if normalized_plan not in ("MONTHLY", "ANNUAL"):
        raise PolicyError("plan must be MONTHLY or ANNUAL")

    if normalized_plan == "MONTHLY":
        if remaining_payments == 0:
            payments: tuple[Payment, ...] = ()
        else:
            weeks = [
                terms.monthly_first_week + index * terms.monthly_cadence_weeks
                for index in range(remaining_payments)
            ]
            if weeks[-1] > context.horizon_weeks:
                raise FlowOutsideHorizon(
                    f"{remaining_payments} remaining monthly payments extend to week "
                    f"{weeks[-1]}, outside the inclusive week 0..{context.horizon_weeks} horizon"
                )
            payments = tuple(Payment(week=week, amount=terms.monthly_price) for week in weeks)
    else:
        if remaining_payments > 1:
            raise PolicyError("an annual plan can have at most one remaining payment")
        payments = (
            (Payment(week=terms.annual_payment_week, amount=terms.annual_price),)
            if remaining_payments == 1
            else ()
        )

    forecast = forecast_plan(context, payments, plan=normalized_plan)
    hold = forecast.cash_safe
    shortfall = forecast.shortfall
    if hold:
        decision = LIVE_HOLD
        reason = (
            f"Confirmed cash stays at or above the {format_money(context.reserve_floor)} "
            f"{context.currency} floor; the minimum is "
            f"{format_money(forecast.confirmed_minimum_balance)} {context.currency} at "
            f"week {forecast.confirmed_minimum_week}."
        )
    else:
        decision = LIVE_FREEZE
        reason = (
            f"Confirmed cash falls {format_money(shortfall)} {context.currency} below "
            f"the {format_money(context.reserve_floor)} {context.currency} floor "
            f"(minimum {format_money(forecast.confirmed_minimum_balance)} "
            f"{context.currency} at week {forecast.confirmed_minimum_week})."
        )

    return {
        "decision": decision,
        "reason": reason,
        "plan": normalized_plan,
        "remaining_payments": remaining_payments,
        "minimum_balance": format_money(forecast.confirmed_minimum_balance),
        "minimum_week": forecast.confirmed_minimum_week,
        "reserve_floor": format_money(context.reserve_floor),
        "shortfall": format_money(shortfall),
        "recommendation_only": True,
    }


def _payments_json(payments: Sequence[Payment]) -> list[dict[str, Any]]:
    return [
        {"week": payment.week, "amount": format_money(payment.amount)}
        for payment in payments
    ]


def _trajectory_json(trajectory: Sequence[tuple[int, Decimal]]) -> list[dict[str, Any]]:
    return [
        {"week": week, "balance": format_money(balance)}
        for week, balance in trajectory
    ]


def policy_output(
    context: CashContext,
    terms: PurchaseTerms,
    *,
    remaining_payments: int | None = None,
) -> dict[str, Any]:
    """Return the flat, prompt-facing policy result.

    The language model may quote these numbers but must never recalculate them.
    """

    evaluation = evaluate_policy(context, terms)
    live_plan: dict[str, Any] | None = None
    if remaining_payments is not None and evaluation.decision != DECISION_DEFER:
        live_plan = evaluate_live_plan(
            context,
            terms,
            remaining_payments,
            plan="ANNUAL" if evaluation.decision == DECISION_ANNUAL else "MONTHLY",
        )

    return {
        "decision": evaluation.decision,
        "reason": evaluation.reason,
        "policy_version": evaluation.policy_version,
        "currency": context.currency,
        "available_balance": format_money(context.available_balance),
        "reserve_floor": format_money(context.reserve_floor),
        "horizon_weeks": context.horizon_weeks,
        "annual_saving": format_money(evaluation.annual_saving),
        "annual_cash_safe": evaluation.annual.cash_safe,
        "annual_confirmed_minimum_balance": format_money(
            evaluation.annual.confirmed_minimum_balance
        ),
        "annual_confirmed_minimum_week": evaluation.annual.confirmed_minimum_week,
        "annual_with_expected_minimum_balance": format_money(
            evaluation.annual.with_expected_minimum_balance
        ),
        "annual_with_expected_minimum_week": evaluation.annual.with_expected_minimum_week,
        "annual_ending_balance": format_money(evaluation.annual.confirmed_ending_balance),
        "monthly_cash_safe": evaluation.monthly.cash_safe,
        "monthly_confirmed_minimum_balance": format_money(
            evaluation.monthly.confirmed_minimum_balance
        ),
        "monthly_confirmed_minimum_week": evaluation.monthly.confirmed_minimum_week,
        "monthly_with_expected_minimum_balance": format_money(
            evaluation.monthly.with_expected_minimum_balance
        ),
        "monthly_with_expected_minimum_week": evaluation.monthly.with_expected_minimum_week,
        "monthly_ending_balance": format_money(evaluation.monthly.confirmed_ending_balance),
        "annual_payments": _payments_json(evaluation.annual.payments),
        "monthly_payments": _payments_json(evaluation.monthly.payments),
        "annual_confirmed_trajectory": _trajectory_json(
            evaluation.annual.confirmed_trajectory
        ),
        "monthly_confirmed_trajectory": _trajectory_json(
            evaluation.monthly.confirmed_trajectory
        ),
        "assumptions": {
            "horizon": (
                f"inclusive {context.horizon_weeks}-week horizon "
                f"(weeks 0 to {context.horizon_weeks})"
            ),
            "monthly_cadence": (
                f"{terms.monthly_cadence_weeks}-week cadence as an approximation "
                "of calendar months"
            ),
            "conservative_case": (
                "expected inflows excluded; expected outflows always counted"
            ),
            "within_week_order": "outflows applied and minimum checked before inflows",
            "safe_balance": "confirmed minimum greater than or equal to the floor",
            "available_balance_source": "wallet available_amount, not total_amount",
            "currency_scope": f"{context.currency} only in the sandbox",
            "merchant_category_scope": (
                f"MCC {terms.merchant_category} only in the sandbox"
            ),
            "synthetic_demo_data": True,
        },
        "live_plan_decision": (
            live_plan["decision"] if live_plan is not None else "NOT_EVALUATED"
        ),
        "live_plan": live_plan,
    }
