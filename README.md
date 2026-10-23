# Mandate — Vercel deployment

A Vercel-ready wrapper for the **Mandate** sandbox prototype.

- Main repository: https://github.com/vikassharma270907-alt/mandate
- Live demo: **https://mandate-vercel.vercel.app**
- Vercel project: https://vercel.com/vikassharma270907-alts-projects/mandate-vercel
- This is a sandbox prototype with synthetic data. It is **not** production banking software.

## Why this wrapper exists

The original Mandate dashboard runs on a stateful local HTTP server. Vercel Functions are stateless, so this deployment carries the demo state in a compact `portable_state` token that round-trips through the browser:

1. The browser loads `/api/state`.
2. Every action POST includes the previous `portable_state`.
3. The Flask function hydrates a fresh `MandateApplication`, applies exactly one action, and returns the next snapshot plus the next state token.

There is no server-side session and no database. Cold starts, multiple function instances, and reloads are therefore safe: a reload simply starts a fresh demo. The token contains only synthetic demo data.

## Stack

- Python 3.12 on Vercel
- Flask (deployment wrapper only)
- The original `mandate` package, copied from the main repository
- `FakeAirwallex` only — the live Airwallex adapter is not used

## Run locally

```powershell
python -m pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

Smoke test the whole flow:

```powershell
python smoke_test.py
```

Verify the deployed URL end to end:

```powershell
python live_smoke.py --url https://mandate-vercel.vercel.app
```

## Deploy

This Vercel project is connected to the `mandate-vercel` GitHub repository. Pushes to `main` deploy automatically. To deploy manually with the Vercel CLI logged in:

```powershell
vercel --prod
```

Or create a temporary deployment without logging in:

```powershell
npx vercel@latest deploy --temporary --yes --project mandate-vercel
```

For a private deployment, set a real passport key:

```powershell
vercel env add MANDATE_PASSPORT_KEY production
```

The public demo falls back to a fixed sandbox-only key. Every value is synthetic, so this is a demo integrity check, not a security boundary.

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask entrypoint, routes, hydration/export of portable state |
| `dashboard.html` | Dashboard with state-token round-tripping |
| `mandate/` | Copy of the deterministic engine, passport, card controls, retry registry |
| `fixtures/` | Synthetic quote and prompt-injection documents |
| `smoke_test.py` | End-to-end local test of the Vercel transport |
| `vercel.json` | Function configuration |

## Keeping it in sync

The `mandate/` directory is copied from the main repository. When the main engine changes, re-copy `mandate/` and `fixtures/` here, then rerun `python smoke_test.py`.

## What remains unconfirmed

Live Airwallex sandbox behaviour is still **UNCONFIRMED**. This deployment uses only the in-memory fake adapter. See the main repository's `docs/SANDBOX_RUNBOOK.md`.
