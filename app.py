"""Vercel wrapper for the Mandate sandbox prototype.

Vercel Functions are stateless, while the Mandate walkthrough is a stateful
sequence.  Every POST therefore carries a compact ``portable_state`` blob from
the browser.  The function hydrates a fresh ``MandateApplication``, applies one
action, and returns the next snapshot plus the next portable state.

This is a demo transport only.  All money is synthetic, the fake Airwallex
adapter is in-memory, and the live Airwallex adapter is not used here.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from mandate.application import ApplicationError, MandateApplication
from mandate.money import format_money
from mandate.passport import ApprovedIntent, Passport, passport_to_dict

BASE_DIR = Path(__file__).resolve().parent
QUOTE_TEXT = (BASE_DIR / "fixtures" / "quote_cloudmetrics.txt").read_text(
    encoding="utf-8"
)
INJECTION_TEXT = (BASE_DIR / "fixtures" / "injection_quote.txt").read_text(
    encoding="utf-8"
)

# Public sandbox demo key.  It is deliberately not a secret: every value here is
# synthetic.  Set MANDATE_PASSPORT_KEY to override it in a private deployment.
PASSPORT_KEY = os.environ.get(
    "MANDATE_PASSPORT_KEY",
    "mandate-vercel-public-sandbox-key-2026",
)
INITIAL_BALANCE = Decimal("32000.00")
RECEIVABLE_AMOUNT = Decimal("12000.00")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024


# ---------------------------------------------------------------------------
# Portable state
# ---------------------------------------------------------------------------
def _new_application(*, received: bool) -> MandateApplication:
    application = MandateApplication(
        quote_text=QUOTE_TEXT,
        injection_text=INJECTION_TEXT,
        initial_balance=INITIAL_BALANCE,
        passport_key=PASSPORT_KEY,
    )
    if received:
        application.adapter.deposit(RECEIVABLE_AMOUNT)
        application.receipt_received = True
    return application


def _intent_from_dict(data: dict[str, Any]) -> ApprovedIntent:
    return ApprovedIntent(
        vendor=data["vendor"],
        amount=Decimal(str(data["amount"])),
        currency=data["currency"],
        billing_cadence=data["billing_cadence"],
        merchant_category=data["merchant_category"],
        evidence_reference=data["evidence_reference"],
        policy_version=data["policy_version"],
    )


def _passport_from_dict(data: dict[str, Any]) -> Passport:
    return Passport(
        approval_id=data["approval_id"],
        vendor=data["vendor"],
        amount=Decimal(str(data["amount"])),
        currency=data["currency"],
        billing_cadence=data["billing_cadence"],
        merchant_category=data["merchant_category"],
        evidence_reference=data["evidence_reference"],
        policy_version=data["policy_version"],
        intent_hash=data["intent_hash"],
        approval_expiry=data["approval_expiry"],
        approval_status=data["approval_status"],
        record_seal=data["record_seal"],
    )


def hydrate(payload: dict[str, Any] | None) -> MandateApplication:
    """Rebuild the demo application from the browser-held state token."""

    if not payload:
        return _new_application(received=False)

    application = _new_application(received=bool(payload.get("receipt_received")))

    proposal = payload.get("proposal")
    if isinstance(proposal, dict):
        proposal = dict(proposal)
        intent_data = proposal.get("submitted_intent")
        proposal["_intent"] = (
            _intent_from_dict(intent_data) if isinstance(intent_data, dict) else None
        )
        application.proposal = proposal

    passport = payload.get("passport")
    if isinstance(passport, dict):
        application.passport = _passport_from_dict(passport)

    card = payload.get("card")
    if isinstance(card, dict):
        application.card = card

    cards = payload.get("cards")
    if isinstance(cards, dict):
        application.adapter._cards = cards

    transactions = payload.get("transactions")
    if isinstance(transactions, list):
        application.adapter._transactions = transactions

    balance = payload.get("available_balance")
    if balance is not None:
        application.adapter._balance = Decimal(str(balance))

    events = payload.get("events")
    if isinstance(events, list):
        application.events = events

    application.last_live_plan = payload.get("live_plan")
    return application


def export_state(application: MandateApplication) -> dict[str, Any]:
    proposal = None
    if application.proposal is not None:
        proposal = {
            key: value
            for key, value in application.proposal.items()
            if not key.startswith("_")
        }
    return {
        "receipt_received": application.receipt_received,
        "proposal": proposal,
        "passport": (
            passport_to_dict(application.passport)
            if application.passport is not None
            else None
        ),
        "card": application.card,
        "cards": application.adapter._cards,
        "transactions": application.adapter.list_transactions()[-20:],
        "available_balance": format_money(application.adapter.available_balance()),
        "events": application.events[-20:],
        "live_plan": application.last_live_plan,
    }


def _body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _application_from_request() -> tuple[MandateApplication, dict[str, Any]]:
    body = _body()
    portable = body.get("portable_state")
    application = hydrate(portable if isinstance(portable, dict) else None)
    return application, body


def _respond(application: MandateApplication, **extra: Any):
    payload = {
        "state": application.snapshot(),
        "portable_state": export_state(application),
    }
    payload.update(extra)
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return send_file(BASE_DIR / "dashboard.html", mimetype="text/html")


@app.get("/api/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "mode": "sandbox",
            "deployment": "vercel",
            "stateless": True,
        }
    )


@app.get("/api/state")
def state():
    return _respond(_new_application(received=False))


@app.post("/api/run")
def run():
    application, _ = _application_from_request()
    application.run_policy()
    return _respond(application)


@app.post("/api/approve")
def approve():
    application, body = _application_from_request()
    token = body.get("approval_token")
    if not isinstance(token, str) or not token:
        raise ApplicationError("approval_token is required")
    application.approve(token)
    return _respond(application)


@app.post("/api/receipt")
def receipt():
    application, _ = _application_from_request()
    application.simulate_receipt()
    return _respond(application)


@app.post("/api/simulate")
def simulate():
    application, body = _application_from_request()
    if "amount" not in body:
        raise ApplicationError("amount is required")
    transaction = application.simulate_authorization(
        body["amount"],
        mcc=body.get("mcc"),
        merchant=body.get("merchant") or "CloudMetrics Inc",
        capture=bool(body.get("capture", False)),
    )
    return _respond(application, transaction=transaction)


@app.post("/api/live-plan")
def live_plan():
    application, _ = _application_from_request()
    result = application.live_plan_check()
    return _respond(application, live_plan=result)


@app.post("/api/injection")
def injection():
    application, _ = _application_from_request()
    result = application.injection_check()
    return _respond(application, result=result)


@app.post("/api/tamper")
def tamper():
    application, _ = _application_from_request()
    result = application.tamper_check()
    return _respond(application, result=result)


@app.post("/api/retry")
def retry():
    application, _ = _application_from_request()
    result = application.retry_check()
    return _respond(application, result=result)


@app.post("/api/reset")
def reset():
    application, _ = _application_from_request()
    application.reset()
    return _respond(application)


@app.errorhandler(ApplicationError)
def handle_application_error(error: ApplicationError):
    return jsonify({"error": str(error)}), 400


@app.errorhandler(Exception)
def handle_unexpected(error: Exception):
    if isinstance(error, HTTPException):
        return error
    app.logger.exception("unhandled Vercel demo error")
    return jsonify({"error": f"unexpected error: {error}"}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
