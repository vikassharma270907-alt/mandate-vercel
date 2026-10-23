"""CLI walkthrough and scenario table for the Mandate sandbox."""

from __future__ import annotations

import sys
import textwrap
from decimal import Decimal
from typing import Any, Iterable

from .application import MandateApplication
from .card_controls import summarize_controls
from .mandate_policy_engine import (
    CashContext,
    Flow,
    FlowKind,
    PurchaseTerms,
    evaluate_live_plan,
    evaluate_policy,
)
from .money import format_money

__all__ = ["run_scenarios", "run_story", "SCENARIOS"]

WIDTH = 78


def _rule(char: str = "-") -> None:
    print(char * WIDTH)


def _title(text: str) -> None:
    print("")
    print(text.upper())
    _rule("=")


def _step(text: str) -> None:
    print("")
    print(f"  {text}")
    _rule()


def _wrap(text: str, indent: str = "  ") -> None:
    print(
        textwrap.fill(
            str(text),
            width=WIDTH,
            initial_indent=indent,
            subsequent_indent=indent,
        )
    )


def _kv(label: str, value: Any) -> None:
    print(f"  {label:<26} {value}")


def _scenario(
    number: int,
    name: str,
    balance: str,
    monthly: str,
    annual: str,
    *,
    flows: Iterable[Flow] = (),
    annual_week: int = 0,
    expected: str,
) -> dict[str, Any]:
    terms = PurchaseTerms(
        vendor="CloudMetrics Inc",
        monthly_price=Decimal(monthly),
        annual_price=Decimal(annual),
        annual_payment_week=annual_week,
    )
    context = CashContext(
        available_balance=Decimal(balance),
        reserve_floor=Decimal("15000.00"),
        flows=tuple(flows),
        horizon_weeks=8,
    )
    return {
        "number": number,
        "name": name,
        "context": context,
        "terms": terms,
        "expected": expected,
    }


SCENARIOS = [
    _scenario(
        1,
        "Initial: 32k, expected 12k at week 4",
        "32000.00",
        "2000.00",
        "19680.00",
        flows=[Flow(FlowKind.EXPECTED_INFLOW, Decimal("12000.00"), 4)],
        expected="PROPOSE_MONTHLY",
    ),
    _scenario(
        2,
        "Receipt landed: 44k, no pending inflow",
        "44000.00",
        "2000.00",
        "19680.00",
        expected="PROPOSE_ANNUAL",
    ),
    _scenario(
        3,
        "Receipt confirmed but scheduled week 4",
        "32000.00",
        "2000.00",
        "19680.00",
        flows=[Flow(FlowKind.CONFIRMED_INFLOW, Decimal("12000.00"), 4)],
        expected="PROPOSE_MONTHLY",
    ),
    _scenario(
        4,
        "Balance 20k",
        "20000.00",
        "2000.00",
        "19680.00",
        expected="DEFER",
    ),
    _scenario(
        5,
        "Receipt confirmed week 3, annual paid week 4",
        "32000.00",
        "2000.00",
        "19680.00",
        flows=[Flow(FlowKind.CONFIRMED_INFLOW, Decimal("12000.00"), 3)],
        annual_week=4,
        expected="PROPOSE_ANNUAL",
    ),
    _scenario(
        6,
        "Annual 25k, no saving (balance 50k)",
        "50000.00",
        "2000.00",
        "25000.00",
        expected="PROPOSE_MONTHLY",
    ),
    _scenario(
        7,
        "Balance 34,680: annual lands exactly on floor",
        "34680.00",
        "2000.00",
        "19680.00",
        expected="PROPOSE_ANNUAL",
    ),
    _scenario(
        8,
        "Balance 34,679.99: one cent under",
        "34679.99",
        "2000.00",
        "19680.00",
        expected="PROPOSE_MONTHLY",
    ),
    _scenario(
        9,
        "17k, expected outflow 3k week 1, 500/1k prices",
        "17000.00",
        "500.00",
        "1000.00",
        flows=[Flow(FlowKind.EXPECTED_OUTFLOW, Decimal("3000.00"), 1)],
        expected="DEFER",
    ),
]


def run_scenarios() -> bool:
    _title("Mandate engine scenarios")
    print(
        "  Synthetic fixtures only. Each row is a fresh context: balance, flows and\n"
        "  prices. The conservative case excludes expected inflows and always counts\n"
        "  expected outflows."
    )
    print("")
    header = (
        f"  {'#':>2}  {'Scenario':<44} {'Decision':<16} {'Annual min':>11} "
        f"{'Monthly min':>12}"
    )
    print(header)
    _rule()
    ok = True
    for item in SCENARIOS:
        evaluation = evaluate_policy(item["context"], item["terms"])
        annual = format_money(evaluation.annual.confirmed_minimum_balance)
        monthly = format_money(evaluation.monthly.confirmed_minimum_balance)
        flag = "ok" if evaluation.decision == item["expected"] else "MISMATCH"
        if evaluation.decision != item["expected"]:
            ok = False
        print(
            f"  {item['number']:>2}  {item['name']:<44} "
            f"{evaluation.decision:<16} {annual:>11} {monthly:>12}  {flag}"
        )
    _rule()
    print(
        f"  {'All nine scenarios match the documented outcomes.' if ok else 'One or more scenarios do not match.'}"
    )
    print("")
    print("  Live-plan re-check (monthly, three payments left):")
    for balance, expected in (
        ("32000.00", "HOLD"),
        ("21000.00", "HOLD"),
        ("20000.00", "RECOMMEND_FREEZE_AND_ESCALATE"),
    ):
        context = CashContext(
            available_balance=Decimal(balance),
            reserve_floor=Decimal("15000.00"),
        )
        result = evaluate_live_plan(context, SCENARIOS[0]["terms"], 3, plan="MONTHLY")
        print(
            f"    balance {balance:>10} -> {result['decision']:<31} "
            f"min {result['minimum_balance']} week {result['minimum_week']}"
            f"  {'ok' if result['decision'] == expected else 'MISMATCH'}"
        )
    print("")
    return ok


def _print_policy(snapshot: dict[str, Any], *, include_reason: bool = True) -> None:
    policy = snapshot["proposal"]["policy"]
    _kv("Decision", policy["decision"])
    _kv("Annual min", f"{policy['annual_confirmed_minimum_balance']} "
                       f"(week {policy['annual_confirmed_minimum_week']})")
    _kv("Monthly min", f"{policy['monthly_confirmed_minimum_balance']} "
                        f"(week {policy['monthly_confirmed_minimum_week']})")
    _kv("Reserve floor", policy["reserve_floor"])
    _kv("Annual saving", policy["annual_saving"])
    if include_reason:
        print("")
        _wrap(policy["reason"])


def run_story(*, include_edges: bool = True, agent: Any | None = None) -> bool:
    _title("Mandate: one request, two cash positions")
    app = MandateApplication(agent=agent)
    snapshot = app.snapshot()

    _step("1. Trusted cash is seeded; the quote is untrusted data")
    _kv("Available balance", snapshot["sandbox"]["available_balance"])
    _kv("Reserve floor", snapshot["sandbox"]["reserve_floor"])
    _kv("Expected receivable", f"{snapshot['receivable']['amount']} at week 4")
    _kv("Vendor", snapshot["quote"]["terms"]["vendor"])
    _kv("Annual / monthly", f"{snapshot['quote']['terms']['annual_price']} / "
                            f"{snapshot['quote']['terms']['monthly_price']}")
    _kv("Quote hash", snapshot["quote"]["sha256"][:32] + "...")

    _step("2. Deterministic policy engine runs before the model explains")
    snapshot = app.run_policy()
    policy = snapshot["proposal"]["policy"]
    _print_policy(snapshot)
    _kv("Agent recommendation", snapshot["proposal"]["agent_output"]["recommendation"])
    _kv("Engine recommendation", policy["decision"])
    if snapshot["proposal"]["agent_output"]["security_flags"]:
        _kv("Security flags", len(snapshot["proposal"]["agent_output"]["security_flags"]))
    else:
        _kv("Security flags", "none")

    _step("3. Human approves the exact terms; passport + card are derived")
    token = snapshot["proposal"]["approval_token"]
    snapshot = app.approve(token)
    passport = snapshot["passport"]
    _kv("Passport", passport["approval_id"])
    _kv("Intent hash", passport["intent_hash"][:24] + "...")
    _kv("Status", passport["validation_status"])
    _kv("Card", snapshot["card"]["id"])
    for line in summarize_controls(snapshot["card"]["controls"]):
        _kv("Control", line)

    _step("4. The card, not the prompt, enforces the limit")
    annual_attempt = app.simulate_authorization("19680.00")
    _kv("Annual $19,680", annual_attempt["status"])
    if annual_attempt["decline_reason"]:
        _wrap(f"Declined: {annual_attempt['decline_reason']}", indent="      ")
    monthly_attempt = app.simulate_authorization("2000.00")
    _kv("Monthly $2,000", monthly_attempt["status"])
    if monthly_attempt["decline_reason"]:
        _wrap(f"Declined: {monthly_attempt['decline_reason']}", indent="      ")
    else:
        _wrap(
            "Approved. This is an authorization-only simulation; no funds move and "
            "available cash is unchanged.",
            indent="      ",
        )

    _step("5. The expected receivable lands; the answer changes")
    snapshot = app.simulate_receipt()
    _kv("New available balance", snapshot["sandbox"]["available_balance"])
    _kv("Receivable status", snapshot["receivable"]["status"])
    snapshot = app.run_policy(trigger="receivable_confirmed")
    _print_policy(snapshot)
    _kv("Proposal status", snapshot["proposal"]["status"])
    _kv("Old card still exists", snapshot["card"]["id"])

    _step("6. The old monthly card cannot authorize the annual charge")
    old_attempt = app.simulate_authorization("19680.00")
    _kv("Annual on old card", old_attempt["status"])
    _wrap(f"Reason: {old_attempt['decline_reason']}", indent="      ")
    _wrap(
        "A fresh human approval and a fresh passport are required for the annual plan.",
        indent="      ",
    )

    _step("7. Fresh approval for the new decision")
    token = snapshot["proposal"]["approval_token"]
    snapshot = app.approve(token)
    _kv("New card", snapshot["card"]["id"])
    _kv("Cadence", snapshot["passport"]["billing_cadence"])
    _kv("Passport status", snapshot["passport"]["validation_status"])
    annual_ok = app.simulate_authorization("19680.00")
    _kv("Annual on new card", annual_ok["status"])
    _wrap(
        "The annual card is single-use, and its all-time cap is the approved amount. "
        "Again, this is an authorization-only sandbox simulation.",
        indent="      ",
    )

    _step("8. Live-plan re-check is recommend-only")
    live = app.live_plan_check()
    _kv("Decision", live["decision"])
    _kv("Minimum", f"{live['minimum_balance']} at week {live['minimum_week']}")
    _wrap(live["reason"], indent="      ")

    if include_edges:
        _step("9. Trust-boundary probes")
        injection = app.injection_check()
        _kv("Injection findings", injection["flagged_count"])
        _kv("Decision unchanged", injection["policy_decision_unchanged"])
        tamper = app.tamper_check()
        _kv("Tampered passport", f"{tamper['tampered_status']} - {tamper['reason']}")
        retry = app.retry_check()
        same = [step for step in retry["steps"] if step.get("same_request_id")]
        _kv("Retry ID reused", "yes" if same and all(s["same_request_id"] for s in same) else "no")
        _kv("Terminal retry blocked", retry["steps"][-1]["blocked"])

    _title("Result")
    _wrap(
        "The same quote produces DEFER, MONTHLY or ANNUAL as cash and timing change. "
        "The model read the document and explained the result. The deterministic "
        "engine made the decision, the human approved exact terms, and the card "
        "enforced the limits. All numbers are synthetic sandbox fixtures."
    )
    print("")
    return True


def run_demo(
    *,
    include_edges: bool = True,
    scenarios_only: bool = False,
    agent: Any | None = None,
) -> int:
    ok = run_scenarios()
    if scenarios_only:
        return 0 if ok else 1
    print("")
    ok = run_story(include_edges=include_edges, agent=agent) and ok
    return 0 if ok else 1
