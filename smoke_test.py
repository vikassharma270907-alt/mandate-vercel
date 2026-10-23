"""Local smoke test for the Vercel wrapper.

Run from the repository root:

    python smoke_test.py
"""

from __future__ import annotations

import json

from app import app

client = app.test_client()
portable_state: dict | None = None
largest_state = 0


def post(path: str, payload: dict | None = None) -> dict:
    global portable_state, largest_state
    body = dict(payload or {})
    if portable_state is not None:
        body["portable_state"] = portable_state
    response = client.post(path, json=body)
    data = response.get_json()
    assert response.status_code == 200, (path, response.status_code, data)
    portable_state = data["portable_state"]
    largest_state = max(largest_state, len(json.dumps(portable_state)))
    return data


def get(path: str) -> dict:
    response = client.get(path)
    data = response.get_json()
    assert response.status_code == 200, (path, response.status_code, data)
    return data


# Initial state
state = get("/api/state")
assert state["state"]["proposal"] is None
assert state["state"]["sandbox"]["available_balance"] == "32000.00"
portable_state = state["portable_state"]
largest_state = len(json.dumps(portable_state))
print("initial state: OK")

# Run policy
data = post("/api/run")
assert data["state"]["proposal"]["decision"] == "PROPOSE_MONTHLY"
token = data["state"]["proposal"]["approval_token"]
print("run policy: PROPOSE_MONTHLY")

# Approve monthly
data = post("/api/approve", {"approval_token": token})
assert data["state"]["passport"]["validation_status"] == "VALID"
assert data["state"]["passport"]["billing_cadence"] == "MONTHLY"
assert data["state"]["card"]["controls"]["transaction_limits"]["limits"][0]["amount"] == 2000.0
print("approve monthly: passport + card OK")

# Card enforcement
data = post("/api/simulate", {"amount": "19680.00"})
assert data["transaction"]["status"] == "DECLINED"
data = post("/api/simulate", {"amount": "2000.00"})
assert data["transaction"]["status"] == "APPROVED"
print("card enforcement: annual declined, monthly approved")

# Security lab
data = post("/api/live-plan")
assert data["live_plan"]["decision"] == "HOLD"
data = post("/api/injection")
assert data["result"]["flagged_count"] == 4
data = post("/api/tamper")
assert data["result"]["tampered_status"] == "INVALID"
data = post("/api/retry")
assert data["result"]["steps"][-1]["blocked"] is True
print("security lab: live-plan, injection, tamper, retry OK")

# Receipt and re-plan
data = post("/api/receipt")
assert data["state"]["sandbox"]["available_balance"] == "44000.00"
assert data["state"]["receivable"]["status"] == "RECEIVED"
data = post("/api/run")
assert data["state"]["proposal"]["decision"] == "PROPOSE_ANNUAL"
token = data["state"]["proposal"]["approval_token"]
print("receipt + re-plan: PROPOSE_ANNUAL")

# Approve annual and use the single-use card
data = post("/api/approve", {"approval_token": token})
assert data["state"]["passport"]["billing_cadence"] == "ANNUAL"
data = post("/api/simulate", {"amount": "19680.00"})
assert data["transaction"]["status"] == "APPROVED"
print("approve annual: single-use card approved")

# Reset
data = post("/api/reset")
assert data["state"]["proposal"] is None
assert data["state"]["sandbox"]["available_balance"] == "32000.00"
print("reset: OK")

print(f"largest portable state: {largest_state:,} bytes")
print("vercel wrapper smoke test: OK")
