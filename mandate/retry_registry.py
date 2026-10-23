"""SQLite retry registry for idempotent Airwallex operations.

One logical operation owns exactly one random request ID.  Concurrent callers
are serialised with ``BEGIN IMMEDIATE`` so two threads cannot mint IDs for the
same operation.  Replays reuse the original ID until the operation succeeds or
is confirmed terminal.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "IN_FLIGHT",
    "FAILED_RETRYABLE",
    "SUCCEEDED",
    "FAILED_TERMINAL",
    "RetryRegistryError",
    "FingerprintMismatch",
    "TerminalOperation",
    "TerminalNotConfirmed",
    "RequestIdMismatch",
    "RetryDecision",
    "RetryRegistry",
    "fingerprint_payload",
]

IN_FLIGHT = "IN_FLIGHT"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
SUCCEEDED = "SUCCEEDED"
FAILED_TERMINAL = "FAILED_TERMINAL"

_STATUSES = {IN_FLIGHT, FAILED_RETRYABLE, SUCCEEDED, FAILED_TERMINAL}
_REPLAYABLE = {IN_FLIGHT, FAILED_RETRYABLE, SUCCEEDED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id       TEXT PRIMARY KEY,
    fingerprint        TEXT NOT NULL,
    request_id         TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL,
    result_json        TEXT,
    terminal_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);
"""


class RetryRegistryError(RuntimeError):
    """Base class for retry registry failures."""


class FingerprintMismatch(RetryRegistryError):
    """The same operation id was presented with different request content."""


class TerminalOperation(RetryRegistryError):
    """The operation is closed and needs a new operation id."""


class TerminalNotConfirmed(RetryRegistryError):
    """FAILED_TERMINAL requires a GET-confirmed terminal state."""


class RequestIdMismatch(RetryRegistryError):
    """The caller tried to change the request id bound to an operation."""


@dataclass(frozen=True)
class RetryDecision:
    operation_id: str
    request_id: str
    status: str
    replay: bool
    result: Any | None = None


def fingerprint_payload(payload: Mapping[str, Any] | list[Any] | str) -> str:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RetryRegistry:
    """Single-node SQLite registry.

    Each method opens a short-lived connection, so the object is safe to share
    across threads.  Intended for a single-node prototype, not a distributed
    deployment.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = str(path)
        self._request_id_factory = request_id_factory or (
            lambda: f"req_{uuid.uuid4().hex}"
        )
        parent = Path(self.path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            if self.path != ":memory:":
                # Set the journal mode once at startup. Re-applying it on every
                # connection can contend with concurrent BEGIN IMMEDIATE calls.
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def begin_operation(
        self,
        operation_id: str,
        fingerprint: str,
        *,
        request_id: str | None = None,
    ) -> RetryDecision:
        """Claim or replay an operation.

        The caller must use the returned request id for every Airwallex call.
        """

        operation_id = self._require_text(operation_id, "operation_id")
        fingerprint = self._require_text(fingerprint, "fingerprint")
        if request_id is not None:
            request_id = self._require_text(request_id, "request_id")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

            if row is None:
                minted = request_id or self._require_text(
                    self._request_id_factory(), "request_id"
                )
                now = _now_iso()
                connection.execute(
                    """
                    INSERT INTO operations
                        (operation_id, fingerprint, request_id, status, result_json,
                         terminal_confirmed, created_at, updated_at)
                    VALUES (?, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (operation_id, fingerprint, minted, IN_FLIGHT, now, now),
                )
                connection.execute("COMMIT")
                return RetryDecision(
                    operation_id=operation_id,
                    request_id=minted,
                    status=IN_FLIGHT,
                    replay=False,
                )

            if row["fingerprint"] != fingerprint:
                connection.execute("ROLLBACK")
                raise FingerprintMismatch(
                    f"operation {operation_id!r} was already bound to a different fingerprint"
                )

            status = row["status"]
            if status == FAILED_TERMINAL:
                connection.execute("ROLLBACK")
                raise TerminalOperation(
                    f"operation {operation_id!r} is closed after a confirmed terminal "
                    "failure; a new operation requires a fresh policy check"
                )
            if status not in _REPLAYABLE:
                connection.execute("ROLLBACK")
                raise RetryRegistryError(f"unexpected stored status {status!r}")

            connection.execute("COMMIT")
            result = json.loads(row["result_json"]) if row["result_json"] else None
            return RetryDecision(
                operation_id=operation_id,
                request_id=row["request_id"],
                status=status,
                replay=True,
                result=result,
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def record_result(
        self,
        operation_id: str,
        request_id: str,
        status: str,
        *,
        result: Any | None = None,
        terminal_confirmed: bool = False,
    ) -> None:
        operation_id = self._require_text(operation_id, "operation_id")
        request_id = self._require_text(request_id, "request_id")
        if status not in _STATUSES:
            raise RetryRegistryError(f"unknown status {status!r}")
        if status == FAILED_TERMINAL and terminal_confirmed is not True:
            raise TerminalNotConfirmed(
                "FAILED_TERMINAL may only be recorded after a GET confirms terminal state"
            )
        if status != FAILED_TERMINAL and terminal_confirmed:
            raise RetryRegistryError("terminal_confirmed is only valid for FAILED_TERMINAL")

        result_json = None
        if result is not None:
            result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RetryRegistryError(f"unknown operation {operation_id!r}")
            if row["request_id"] != request_id:
                connection.execute("ROLLBACK")
                raise RequestIdMismatch(
                    f"operation {operation_id!r} is bound to request id "
                    f"{row['request_id']!r}, not {request_id!r}"
                )
            if row["status"] == SUCCEEDED and status != SUCCEEDED:
                connection.execute("ROLLBACK")
                raise RetryRegistryError("a SUCCEEDED operation cannot transition to failure")
            if row["status"] == FAILED_TERMINAL:
                connection.execute("ROLLBACK")
                raise TerminalOperation(
                    f"operation {operation_id!r} is already terminal"
                )

            connection.execute(
                """
                UPDATE operations
                   SET status = ?, result_json = ?, terminal_confirmed = ?, updated_at = ?
                 WHERE operation_id = ?
                """,
                (
                    status,
                    result_json,
                    1 if status == FAILED_TERMINAL else 0,
                    _now_iso(),
                    operation_id,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get(self, operation_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (self._require_text(operation_id, "operation_id"),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "operation_id": row["operation_id"],
            "fingerprint": row["fingerprint"],
            "request_id": row["request_id"],
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "terminal_confirmed": bool(row["terminal_confirmed"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _require_text(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RetryRegistryError(f"{field_name} must be a non-empty string")
        return value.strip()
