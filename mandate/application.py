"""Application orchestration for the Mandate sandbox prototype.

The application is the trusted layer.  It assembles the model envelope, calls
the deterministic engine, validates model JSON, binds human approval, issues the
passport, derives card controls and talks to the sandbox adapter.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent import Agent, AgentError, DemoAgent, build_envelope, scan_document
from .card_controls import CardControlError, card_controls_from_passport
from .mandate_policy_engine import (
    POLICY_VERSION,
    CashContext,
    Flow,
    FlowKind,
    InvalidTerms,
    PurchaseTerms,
    evaluate_live_plan,
    policy_output,
)
from .money import MoneyError, format_money, parse_money
from .passport import (
    STATUS_VALID,
    ApprovedIntent,
    Passport,
    intent_to_dict,
    issue_passport,
    passport_to_dict,
    validate_passport,
)
from .retry_registry import (
    FAILED_RETRYABLE,
    FAILED_TERMINAL,
    SUCCEEDED,
    RetryRegistry,
    TerminalOperation,
    fingerprint_payload,
)
from .sandbox import CardAdapter, FakeAirwallex

__all__ = [
    "ApplicationError",
    "DEFAULT_QUOTE_PATH",
    "DEFAULT_INJECTION_PATH",
    "DEFAULT_TERMS",
    "MandateApplication",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUOTE_PATH = PROJECT_ROOT / "fixtures" / "quote_cloudmetrics.txt"
DEFAULT_INJECTION_PATH = PROJECT_ROOT / "fixtures" / "injection_quote.txt"
RESERVE_FLOOR = Decimal("15000.00")
CARD_MAX_AMOUNT = Decimal("25000.00")
INITIAL_BALANCE = Decimal("32000.00")
RECEIVABLE_AMOUNT = Decimal("12000.00")
RECEIVABLE_WEEK = 4
HORIZON_WEEKS = 8

DEFAULT_TERMS = PurchaseTerms(
    vendor="CloudMetrics Inc",
    monthly_price=Decimal("2000.00"),
    annual_price=Decimal("19680.00"),
    currency="USD",
    merchant_category="5734",
    monthly_first_week=0,
    annual_payment_week=0,
    monthly_cadence_weeks=4,
)


class ApplicationError(RuntimeError):
    """A requested application action is not valid in the current state."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _terms_to_dict(terms: PurchaseTerms) -> dict[str, Any]:
    return {
        "vendor": terms.vendor,
        "monthly_price": format_money(terms.monthly_price),
        "annual_price": format_money(terms.annual_price),
        "currency": terms.currency,
        "merchant_category": terms.merchant_category,
        "monthly_first_week": terms.monthly_first_week,
        "annual_payment_week": terms.annual_payment_week,
        "monthly_cadence_weeks": terms.monthly_cadence_weeks,
    }


def _approval_token(proposal_id: str, intent: ApprovedIntent) -> str:
    payload = json.dumps(
        {"proposal_id": proposal_id, "intent": intent_to_dict(intent)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


class MandateApplication:
    """Stateful sandbox walkthrough behind both the CLI and web dashboard."""

    def __init__(
        self,
        *,
        adapter: CardAdapter | None = None,
        agent: Agent | None = None,
        terms: PurchaseTerms | None = None,
        quote_text: str | None = None,
        injection_text: str | None = None,
        initial_balance: Decimal | str | int | float = INITIAL_BALANCE,
        receivable_amount: Decimal | str | int | float = RECEIVABLE_AMOUNT,
        receivable_week: int = RECEIVABLE_WEEK,
        reserve_floor: Decimal | str | int | float = RESERVE_FLOOR,
        passport_key: bytes | str | None = None,
        now_fn: Callable[[], datetime] | None = None,
        retry_db_path: str | Path | None = None,
        card_max_amount: Decimal | str | int | float = CARD_MAX_AMOUNT,
    ) -> None:
        self.now_fn = now_fn or _utc_now
        self.terms = terms or DEFAULT_TERMS
        self.quote_text = (
            quote_text
            if quote_text is not None
            else DEFAULT_QUOTE_PATH.read_text(encoding="utf-8")
        )
        self.injection_text = (
            injection_text
            if injection_text is not None
            else DEFAULT_INJECTION_PATH.read_text(encoding="utf-8")
        )
        self.initial_balance = parse_money(initial_balance, field="initial_balance")
        self.receivable_amount = parse_money(
            receivable_amount, field="receivable_amount"
        )
        self.receivable_week = receivable_week
        self.reserve_floor = parse_money(reserve_floor, field="reserve_floor")
        self.card_max_amount = parse_money(card_max_amount, field="card_max_amount")
        self.agent = agent or DemoAgent()
        self.adapter = adapter or FakeAirwallex(self.initial_balance, now_fn=self.now_fn)

        key = passport_key
        if key is None:
            key = os.environ.get("MANDATE_PASSPORT_KEY") or secrets.token_bytes(32)
        self.passport_key = key

        db_path = retry_db_path or (
            Path(tempfile.gettempdir())
            / f"mandate-retry-{uuid.uuid4().hex}.sqlite"
        )
        self.registry = RetryRegistry(db_path)

        self.quote_reference = self._quote_reference(self.quote_text)
        self.quote_sha256 = sha256(self.quote_text.encode("utf-8")).hexdigest()
        self.evidence_reference = (
            f"quote:{self.quote_reference};sha256:{self.quote_sha256[:16]}"
        )

        self.receipt_received = False
        self.proposal: dict[str, Any] | None = None
        self.passport: Passport | None = None
        self.card: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.last_live_plan: dict[str, Any] | None = None
        self.lock = threading.RLock()

        self._event(
            "sandbox.seeded",
            f"Seeded {format_money(self.initial_balance)} USD of synthetic sandbox cash.",
            {
                "available_balance": format_money(self.initial_balance),
                "reserve_floor": format_money(self.reserve_floor),
            },
        )

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _quote_reference(text: str) -> str:
        for line in text.splitlines():
            if line.lower().startswith("quote:"):
                return line.split(":", 1)[1].strip() or "CM-2026-118"
        return "CM-2026-118"

    def _event(self, kind: str, message: str, data: Mapping[str, Any] | None = None) -> None:
        self.events.append(
            {
                "at": _iso(self.now_fn()),
                "kind": kind,
                "message": message,
                "data": dict(data or {}),
            }
        )
        self.events = self.events[-80:]

    def _trusted_context(self) -> CashContext:
        flows: list[Flow] = []
        if not self.receipt_received:
            flows.append(
                Flow(
                    kind=FlowKind.EXPECTED_INFLOW,
                    amount=self.receivable_amount,
                    week=self.receivable_week,
                    label="Expected customer receivable",
                )
            )
        return CashContext(
            available_balance=self.adapter.available_balance(),
            reserve_floor=self.reserve_floor,
            flows=tuple(flows),
            horizon_weeks=HORIZON_WEEKS,
            currency="USD",
        )

    def _purchase_request(self) -> dict[str, Any]:
        return {
            "vendor": self.terms.vendor,
            "monthly_price": format_money(self.terms.monthly_price),
            "annual_price": format_money(self.terms.annual_price),
            "currency": self.terms.currency,
            "merchant_category": self.terms.merchant_category,
            "monthly_cadence_weeks": self.terms.monthly_cadence_weeks,
            "renewal_date": "2026-11-20",
            "evidence_reference": self.evidence_reference,
        }

    def _proposal_intent(self, decision: str) -> ApprovedIntent | None:
        if decision == "PROPOSE_ANNUAL":
            cadence = "ANNUAL"
            amount = self.terms.annual_price
        elif decision == "PROPOSE_MONTHLY":
            cadence = "MONTHLY"
            amount = self.terms.monthly_price
        else:
            return None
        return ApprovedIntent(
            vendor=self.terms.vendor,
            amount=amount,
            currency=self.terms.currency,
            billing_cadence=cadence,
            merchant_category=self.terms.merchant_category,
            evidence_reference=self.evidence_reference,
            policy_version=POLICY_VERSION,
        )

    # ------------------------------------------------------------------
    # Policy + agent
    # ------------------------------------------------------------------
    def run_policy(self, trigger: str = "initial") -> dict[str, Any]:
        with self.lock:
            context = self._trusted_context()
            try:
                policy = policy_output(context, self.terms)
            except (InvalidTerms, MoneyError) as exc:
                raise ApplicationError(f"policy engine rejected the inputs: {exc}") from exc

            passport_status = "NOT_PRESENT"
            passport_json = None
            if self.passport is not None:
                validation = validate_passport(
                    self.passport, self.passport_key, now=self.now_fn()
                )
                passport_status = validation.status
                passport_json = passport_to_dict(self.passport)

            envelope = build_envelope(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                trigger=trigger,
                trusted_context={
                    "available_balance": format_money(context.available_balance),
                    "reserve_floor": format_money(context.reserve_floor),
                    "horizon_weeks": context.horizon_weeks,
                    "currency": context.currency,
                    "flows": [
                        {
                            "kind": flow.kind.value,
                            "amount": format_money(flow.amount),
                            "week": flow.week,
                            "label": flow.label,
                        }
                        for flow in context.flows
                    ],
                    "purchase_request": self._purchase_request(),
                },
                policy_result=policy,
                passport=passport_json,
                passport_status=passport_status,
                documents=[
                    {
                        "id": "doc-quote-01",
                        "mime_type": "text/plain",
                        "text": self.quote_text,
                    }
                ],
            )

            try:
                agent_output = self.agent.analyze(envelope)
            except AgentError as exc:
                raise ApplicationError(f"agent output was rejected: {exc}") from exc

            if agent_output["recommendation"] != policy["decision"]:
                agent_output["discrepancies"].append(
                    {
                        "field": "recommendation",
                        "document_value": agent_output["recommendation"],
                        "trusted_value": policy["decision"],
                        "action": "POLICY_ENGINE_OVERRIDES_MODEL",
                    }
                )
                agent_output["security_flags"].append(
                    {
                        "type": "MODEL_POLICY_DISAGREEMENT",
                        "source": "agent",
                        "line": 0,
                        "excerpt": (
                            f"model recommended {agent_output['recommendation']}; "
                            f"engine decided {policy['decision']}"
                        ),
                        "disposition": "ENGINE_DECISION_IS_AUTHORITATIVE",
                    }
                )

            intent = self._proposal_intent(policy["decision"])
            proposal_id = f"prop-{uuid.uuid4().hex[:10]}"
            token = _approval_token(proposal_id, intent) if intent else None

            proposal: dict[str, Any] = {
                "id": proposal_id,
                "created_at": _iso(self.now_fn()),
                "trigger": trigger,
                "decision": policy["decision"],
                "status": (
                    "PENDING_APPROVAL"
                    if policy["decision"] != "DEFER"
                    else "DEFERRED"
                ),
                "policy": policy,
                "agent_output": agent_output,
                "terms": _terms_to_dict(self.terms),
                "submitted_intent": intent_to_dict(intent) if intent else None,
                "approval_token": token,
                "passport_id": None,
                "card_id": None,
                "stale_reason": None,
                "_intent": intent,
            }
            self.proposal = proposal
            self.last_live_plan = None
            self._event(
                "policy.evaluated",
                f"Policy engine selected {policy['decision']}.",
                {
                    "decision": policy["decision"],
                    "annual_saving": policy["annual_saving"],
                    "annual_minimum": policy["annual_confirmed_minimum_balance"],
                    "monthly_minimum": policy["monthly_confirmed_minimum_balance"],
                },
            )
            return self.snapshot()

    def refresh_policy(self) -> dict[str, Any]:
        return self.run_policy(trigger="manual_refresh")

    # ------------------------------------------------------------------
    # Human approval + card issuance
    # ------------------------------------------------------------------
    def approve(self, approval_token: str) -> dict[str, Any]:
        with self.lock:
            if self.proposal is None:
                raise ApplicationError("run the policy engine before approving")
            proposal = self.proposal
            if proposal["status"] == "STALE":
                raise ApplicationError(
                    "terms or cash changed; re-run the policy engine before approving"
                )
            if proposal["status"] != "PENDING_APPROVAL":
                raise ApplicationError(
                    f"proposal is {proposal['status']}, not pending approval"
                )
            intent = proposal.get("_intent")
            if not isinstance(intent, ApprovedIntent):
                raise ApplicationError("proposal has no approvable intent")
            expected_token = _approval_token(proposal["id"], intent)
            if not isinstance(approval_token, str) or not secrets.compare_digest(
                approval_token, expected_token
            ):
                raise ApplicationError(
                    "approval did not bind to the exact terms shown; refresh and try again"
                )

            now = self.now_fn()
            passport = issue_passport(
                intent,
                self.passport_key,
                approval_id=f"apr_{uuid.uuid4().hex[:12]}",
                expires_in=timedelta(minutes=15),
                now=now,
            )
            validation = validate_passport(
                passport,
                self.passport_key,
                now=now,
                expected_intent=intent,
            )
            if validation.status != STATUS_VALID:
                raise ApplicationError(
                    f"issued passport failed validation: {validation.status} "
                    f"{validation.reason}"
                )

            try:
                controls = card_controls_from_passport(
                    passport,
                    self.passport_key,
                    now=now,
                    expected_intent=intent,
                    max_amount=self.card_max_amount,
                )
            except CardControlError as exc:
                raise ApplicationError(f"card controls rejected: {exc}") from exc

            operation_id = f"create-card:{proposal['id']}"
            fingerprint = fingerprint_payload(controls)
            decision = self.registry.begin_operation(operation_id, fingerprint)
            card: dict[str, Any] | None = None
            if decision.replay and decision.status == SUCCEEDED and decision.result:
                card = dict(decision.result)
            if card is None:
                try:
                    card = self.adapter.create_card(
                        controls, request_id=decision.request_id
                    )
                except Exception as exc:
                    self.registry.record_result(
                        operation_id,
                        decision.request_id,
                        FAILED_RETRYABLE,
                        result={"error": str(exc)},
                    )
                    raise ApplicationError(f"card creation failed: {exc}") from exc
                self.registry.record_result(
                    operation_id,
                    decision.request_id,
                    SUCCEEDED,
                    result=card,
                )

            self.passport = passport
            self.card = card
            proposal["status"] = "APPROVED"
            proposal["passport_id"] = passport.approval_id
            proposal["card_id"] = card.get("id")
            proposal["approved_at"] = _iso(now)
            proposal["controls"] = controls
            self._event(
                "passport.issued",
                f"Human approved {passport.billing_cadence} terms; sealed passport issued.",
                {
                    "approval_id": passport.approval_id,
                    "intent_hash": passport.intent_hash,
                    "expires": passport.approval_expiry,
                },
            )
            self._event(
                "card.created",
                f"Virtual card {card.get('id')} created with card-enforced controls.",
                {"controls": controls, "request_id": decision.request_id},
            )
            return self.snapshot()

    # ------------------------------------------------------------------
    # Sandbox actions
    # ------------------------------------------------------------------
    def simulate_receipt(self) -> dict[str, Any]:
        with self.lock:
            if self.receipt_received:
                raise ApplicationError("the synthetic receivable has already landed")
            result = self.adapter.deposit(self.receivable_amount)
            self.receipt_received = True
            if self.proposal is not None and self.proposal["status"] == "PENDING_APPROVAL":
                self.proposal["status"] = "STALE"
                self.proposal["stale_reason"] = (
                    "confirmed cash changed after the receivable landed; re-run the policy engine"
                )
            self._event(
                "sandbox.deposit",
                (
                    f"Synthetic customer receipt of {format_money(self.receivable_amount)} "
                    "USD landed."
                ),
                result,
            )
            return self.snapshot()

    def simulate_authorization(
        self,
        amount: Decimal | str | int | float,
        *,
        mcc: str | None = None,
        merchant: str = "CloudMetrics Inc",
        capture: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            if self.card is None:
                raise ApplicationError("approve a proposal and create a card first")
            try:
                transaction = self.adapter.authorize(
                    self.card["id"],
                    amount,
                    currency="USD",
                    mcc=mcc or self.terms.merchant_category,
                    merchant=merchant,
                    capture=capture,
                    now=self.now_fn(),
                )
            except Exception as exc:
                raise ApplicationError(f"authorization failed: {exc}") from exc
            self._event(
                "card.authorization",
                (
                    f"{transaction.get('status')}: {transaction.get('amount')} USD "
                    f"at {transaction.get('merchant')}."
                ),
                transaction,
            )
            return transaction

    def live_plan_check(self) -> dict[str, Any]:
        with self.lock:
            context = self._trusted_context()
            decision = (
                self.proposal["decision"] if self.proposal is not None else "PROPOSE_MONTHLY"
            )
            if decision == "PROPOSE_ANNUAL":
                result = evaluate_live_plan(
                    context, self.terms, 1, plan="ANNUAL"
                )
            else:
                result = evaluate_live_plan(
                    context, self.terms, 3, plan="MONTHLY"
                )
            self.last_live_plan = result
            self._event(
                "live_plan.checked",
                f"Live-plan re-check: {result['decision']}.",
                result,
            )
            return result

    # ------------------------------------------------------------------
    # Edge-case demonstrations
    # ------------------------------------------------------------------
    def tamper_check(self) -> dict[str, Any]:
        with self.lock:
            if self.passport is None:
                raise ApplicationError("approve a proposal before running the tamper test")
            original = passport_to_dict(self.passport)
            tampered_data = dict(original)
            tampered_data["amount"] = format_money(self.passport.amount + Decimal("100.00"))
            tampered_data["approval_status"] = "APPROVED"
            tampered = Passport(
                approval_id=tampered_data["approval_id"],
                vendor=tampered_data["vendor"],
                amount=Decimal(tampered_data["amount"]),
                currency=tampered_data["currency"],
                billing_cadence=tampered_data["billing_cadence"],
                merchant_category=tampered_data["merchant_category"],
                evidence_reference=tampered_data["evidence_reference"],
                policy_version=tampered_data["policy_version"],
                intent_hash=tampered_data["intent_hash"],
                approval_expiry=tampered_data["approval_expiry"],
                approval_status=tampered_data["approval_status"],
                record_seal=tampered_data["record_seal"],
            )
            validation = validate_passport(
                tampered, self.passport_key, now=self.now_fn()
            )
            self._event(
                "passport.tamper_test",
                f"Tampered passport rejected as {validation.status}.",
                {"reason": validation.reason},
            )
            return {
                "original_status": "VALID",
                "tampered_status": validation.status,
                "reason": validation.reason,
                "changed_field": "amount",
                "original_amount": original["amount"],
                "tampered_amount": tampered_data["amount"],
            }

    def injection_check(self) -> dict[str, Any]:
        with self.lock:
            context = self._trusted_context()
            policy = policy_output(context, self.terms)
            flags = scan_document(self.injection_text, "doc-injection-demo")
            self._event(
                "security.injection_test",
                f"Prompt-injection document flagged with {len(flags)} finding(s); terms unchanged.",
                {"flags": flags},
            )
            return {
                "flags": flags,
                "flagged_count": len(flags),
                "policy_decision_unchanged": policy["decision"],
                "card_limit_unchanged": (
                    self.card["controls"]["transaction_limits"]["limits"][0]["amount"]
                    if self.card
                    else None
                ),
                "disposition": "UNTRUSTED_TEXT_TREATED_AS_DATA",
            }

    def retry_check(self) -> dict[str, Any]:
        with self.lock:
            operation_id = f"demo-retry-{uuid.uuid4().hex[:8]}"
            fingerprint = fingerprint_payload({"demo": "request", "operation_id": operation_id})
            steps: list[dict[str, Any]] = []

            first = self.registry.begin_operation(operation_id, fingerprint)
            steps.append(
                {
                    "step": "first call",
                    "request_id": first.request_id,
                    "status": first.status,
                    "replay": first.replay,
                }
            )
            second = self.registry.begin_operation(operation_id, fingerprint)
            steps.append(
                {
                    "step": "retry while in flight",
                    "request_id": second.request_id,
                    "status": second.status,
                    "replay": second.replay,
                    "same_request_id": second.request_id == first.request_id,
                }
            )
            self.registry.record_result(
                operation_id,
                first.request_id,
                FAILED_RETRYABLE,
                result={"http_status": 503, "error": "sandbox timeout"},
            )
            third = self.registry.begin_operation(operation_id, fingerprint)
            steps.append(
                {
                    "step": "retry after retryable failure",
                    "request_id": third.request_id,
                    "status": third.status,
                    "replay": third.replay,
                    "same_request_id": third.request_id == first.request_id,
                }
            )
            self.registry.record_result(
                operation_id,
                first.request_id,
                FAILED_TERMINAL,
                result={"http_status": 404, "confirmed_by": "GET"},
                terminal_confirmed=True,
            )
            terminal_blocked = False
            try:
                self.registry.begin_operation(operation_id, fingerprint)
            except TerminalOperation:
                terminal_blocked = True
            steps.append(
                {
                    "step": "retry after terminal failure",
                    "blocked": terminal_blocked,
                    "requires_new_operation": True,
                }
            )
            self._event(
                "retry.test",
                "Retry registry reused one request ID and closed the terminal operation.",
                {"steps": steps},
            )
            return {"operation_id": operation_id, "steps": steps}

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = self.now_fn()
            passport_json = None
            passport_status = "NOT_PRESENT"
            intent_status = None
            if self.passport is not None:
                validation = validate_passport(self.passport, self.passport_key, now=now)
                passport_status = validation.status
                passport_json = passport_to_dict(self.passport)
                passport_json["validation_status"] = validation.status
                passport_json["validation_reason"] = validation.reason
                if self.proposal and self.proposal.get("_intent") is not None:
                    intent_validation = validate_passport(
                        self.passport,
                        self.passport_key,
                        now=now,
                        expected_intent=self.proposal["_intent"],
                    )
                    intent_status = intent_validation.status

            proposal_json = None
            if self.proposal is not None:
                proposal_json = {
                    key: value
                    for key, value in self.proposal.items()
                    if not key.startswith("_")
                }

            live_plan = self.last_live_plan
            if live_plan is None and self.proposal is not None:
                try:
                    context = self._trusted_context()
                    plan = (
                        "ANNUAL"
                        if self.proposal["decision"] == "PROPOSE_ANNUAL"
                        else "MONTHLY"
                    )
                    remaining = 1 if plan == "ANNUAL" else 3
                    live_plan = evaluate_live_plan(
                        context, self.terms, remaining, plan=plan
                    )
                except Exception:
                    live_plan = None

            return {
                "now": _iso(now),
                "mode": "sandbox",
                "synthetic": True,
                "policy_version": POLICY_VERSION,
                "agent_mode": getattr(self.agent, "mode", "unknown"),
                "sandbox": {
                    "available_balance": format_money(self.adapter.available_balance()),
                    "reserve_floor": format_money(self.reserve_floor),
                    "currency": "USD",
                    "horizon_weeks": HORIZON_WEEKS,
                },
                "receivable": {
                    "amount": format_money(self.receivable_amount),
                    "week": self.receivable_week,
                    "received": self.receipt_received,
                    "status": "RECEIVED" if self.receipt_received else "EXPECTED",
                },
                "quote": {
                    "reference": self.quote_reference,
                    "sha256": self.quote_sha256,
                    "evidence_reference": self.evidence_reference,
                    "terms": _terms_to_dict(self.terms),
                    "text": self.quote_text,
                },
                "proposal": proposal_json,
                "passport": passport_json,
                "passport_status": passport_status,
                "passport_matches_current_intent": intent_status,
                "card": self.card,
                "transactions": self.adapter.list_transactions(),
                "live_plan": live_plan,
                "events": list(self.events),
            }

    def reset(self) -> dict[str, Any]:
        with self.lock:
            if not isinstance(self.adapter, FakeAirwallex):
                raise ApplicationError("reset is only available with the fake sandbox")
            self.adapter = FakeAirwallex(self.initial_balance, now_fn=self.now_fn)
            self.receipt_received = False
            self.proposal = None
            self.passport = None
            self.card = None
            self.last_live_plan = None
            self.events = []
            self._event(
                "sandbox.reset",
                f"Sandbox reset with {format_money(self.initial_balance)} USD synthetic cash.",
                {"reserve_floor": format_money(self.reserve_floor)},
            )
            return self.snapshot()
