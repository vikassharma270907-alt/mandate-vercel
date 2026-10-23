"""Local web dashboard for the Mandate sandbox demo (stdlib only)."""

from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .application import ApplicationError, MandateApplication

__all__ = ["serve", "make_server", "STATIC_DIR"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"
MAX_BODY_BYTES = 64 * 1024


class MandateHandler(BaseHTTPRequestHandler):
    server_version = "MandateSandbox/2.2"
    application: MandateApplication

    # -- helpers ---------------------------------------------------------
    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length > MAX_BODY_BYTES:
            raise ApplicationError("request body too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplicationError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApplicationError("JSON body must be an object")
        return payload

    def _authorize_local(self) -> bool:
        # The dashboard binds to localhost and is a demo, but reject obvious
        # cross-origin browser writes. This is not a production CSRF defence.
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost"):
                return False
        return True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep the terminal quiet during the demo.
        return

    # -- GET -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "dashboard file missing")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok", "mode": "sandbox"})
            return
        if path == "/api/state":
            try:
                self._json(HTTPStatus.OK, self.application.snapshot())
            except Exception as exc:  # pragma: no cover - defensive
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    # -- POST ------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._authorize_local():
            self._error(HTTPStatus.FORBIDDEN, "cross-origin request rejected")
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/run":
                self._json(HTTPStatus.OK, {"state": self.application.run_policy()})
            elif path == "/api/approve":
                token = body.get("approval_token")
                if not isinstance(token, str) or not token:
                    raise ApplicationError("approval_token is required")
                self._json(HTTPStatus.OK, {"state": self.application.approve(token)})
            elif path == "/api/receipt":
                self._json(HTTPStatus.OK, {"state": self.application.simulate_receipt()})
            elif path == "/api/simulate":
                if "amount" not in body:
                    raise ApplicationError("amount is required")
                transaction = self.application.simulate_authorization(
                    body["amount"],
                    mcc=body.get("mcc"),
                    merchant=body.get("merchant") or "CloudMetrics Inc",
                    capture=bool(body.get("capture", False)),
                )
                self._json(
                    HTTPStatus.OK,
                    {"transaction": transaction, "state": self.application.snapshot()},
                )
            elif path == "/api/live-plan":
                result = self.application.live_plan_check()
                self._json(
                    HTTPStatus.OK,
                    {"live_plan": result, "state": self.application.snapshot()},
                )
            elif path == "/api/tamper":
                result = self.application.tamper_check()
                self._json(
                    HTTPStatus.OK,
                    {"result": result, "state": self.application.snapshot()},
                )
            elif path == "/api/injection":
                result = self.application.injection_check()
                self._json(
                    HTTPStatus.OK,
                    {"result": result, "state": self.application.snapshot()},
                )
            elif path == "/api/retry":
                result = self.application.retry_check()
                self._json(
                    HTTPStatus.OK,
                    {"result": result, "state": self.application.snapshot()},
                )
            elif path == "/api/reset":
                self._json(HTTPStatus.OK, {"state": self.application.reset()})
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except ApplicationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"unexpected error: {exc}")

    # -- misc ------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()


def make_server(
    application: MandateApplication,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundMandateHandler",
        (MandateHandler,),
        {"application": application},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    application: MandateApplication | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    agent: Any | None = None,
) -> None:
    app = application or MandateApplication(agent=agent)
    server = make_server(app, host, port)
    url = f"http://{host}:{port}"
    print("")
    print("  MANDATE  |  sandbox prototype  |  synthetic data only")
    print(f"  Dashboard: {url}")
    print("  Ctrl+C to stop")
    print("")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Mandate dashboard.")
    finally:
        server.shutdown()
        server.server_close()
