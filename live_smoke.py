"""Run the full Mandate walkthrough against a deployed Vercel URL.

    python live_smoke.py --url https://mandate-vercel.vercel.app
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

portable_state: dict | None = None
largest_state = 0


def call(base: str, path: str, payload: dict | None = None) -> dict:
    global portable_state, largest_state
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = dict(payload)
        if portable_state is not None:
            data["portable_state"] = portable_state
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} failed with HTTP {exc.code}: {detail}") from exc
    data = json.loads(raw)
    if "portable_state" in data:
        portable_state = data["portable_state"]
        largest_state = max(largest_state, len(json.dumps(portable_state)))
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://mandate-vercel.vercel.app")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    health = call(base, "/api/health")
    assert health["status"] == "ok" and health["stateless"] is True
    print("health: OK")

    state = call(base, "/api/state")
    assert state["state"]["proposal"] is None
    assert state["state"]["sandbox"]["available_balance"] == "32000.00"
    print("initial state: OK")

    data = call(base, "/api/run", {})
    assert data["state"]["proposal"]["decision"] == "PROPOSE_MONTHLY"
    token = data["state"]["proposal"]["approval_token"]
    print("run policy: PROPOSE_MONTHLY")

    data = call(base, "/api/approve", {"approval_token": token})
    assert data["state"]["passport"]["validation_status"] == "VALID"
    assert data["state"]["passport"]["billing_cadence"] == "MONTHLY"
    print("approve monthly: passport + card OK")

    data = call(base, "/api/simulate", {"amount": "19680.00"})
    assert data["transaction"]["status"] == "DECLINED"
    data = call(base, "/api/simulate", {"amount": "2000.00"})
    assert data["transaction"]["status"] == "APPROVED"
    print("card enforcement: annual declined, monthly approved")

    data = call(base, "/api/live-plan", {})
    assert data["live_plan"]["decision"] == "HOLD"
    data = call(base, "/api/injection", {})
    assert data["result"]["flagged_count"] == 4
    data = call(base, "/api/tamper", {})
    assert data["result"]["tampered_status"] == "INVALID"
    data = call(base, "/api/retry", {})
    assert data["result"]["steps"][-1]["blocked"] is True
    print("security lab: live-plan, injection, tamper, retry OK")

    data = call(base, "/api/receipt", {})
    assert data["state"]["sandbox"]["available_balance"] == "44000.00"
    assert data["state"]["receivable"]["status"] == "RECEIVED"
    data = call(base, "/api/run", {})
    assert data["state"]["proposal"]["decision"] == "PROPOSE_ANNUAL"
    token = data["state"]["proposal"]["approval_token"]
    print("receipt + re-plan: PROPOSE_ANNUAL")

    data = call(base, "/api/approve", {"approval_token": token})
    assert data["state"]["passport"]["billing_cadence"] == "ANNUAL"
    data = call(base, "/api/simulate", {"amount": "19680.00"})
    assert data["transaction"]["status"] == "APPROVED"
    print("approve annual: single-use card approved")

    data = call(base, "/api/reset", {})
    assert data["state"]["proposal"] is None
    assert data["state"]["sandbox"]["available_balance"] == "32000.00"
    print("reset: OK")

    print(f"largest portable state: {largest_state:,} bytes")
    print("live Vercel smoke test: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
