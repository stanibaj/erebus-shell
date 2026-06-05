"""SQLite-backed durable state. The only module that touches the database."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from erebus.state.models import (
    AuditEntry,
    PendingRequest,
    RequestStatus,
    Run,
    RunStatus,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

    def init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_requests (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                command TEXT NOT NULL,
                justification TEXT NOT NULL,
                status TEXT NOT NULL,
                ticket_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                event TEXT NOT NULL,
                command TEXT,
                decision TEXT,
                detail TEXT
            );
            """
        )
        self._conn.commit()

    # ---- runs ---------------------------------------------------------
    def create_run(self, agent: str, task: str) -> str:
        run_id = _new_id()
        ts = _now()
        self._conn.execute(
            "INSERT INTO runs (id, agent, task, status, session_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (run_id, agent, task, RunStatus.RUNNING.value, ts, ts),
        )
        self._conn.commit()
        return run_id

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return Run(
            id=row["id"], agent=row["agent"], task=row["task"],
            status=RunStatus(row["status"]), session_id=row["session_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def set_run_status(self, run_id: str, status: RunStatus) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, _now(), run_id),
        )
        self._conn.commit()

    def set_run_session(self, run_id: str, session_id: str) -> None:
        self._conn.execute(
            "UPDATE runs SET session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, _now(), run_id),
        )
        self._conn.commit()

    # ---- pending requests --------------------------------------------
    def create_pending_request(self, run_id: str, command: str, justification: str,
                               ticket_id: str, expires_at: str) -> str:
        req_id = _new_id()
        self._conn.execute(
            "INSERT INTO pending_requests "
            "(id, run_id, command, justification, status, ticket_id, created_at, expires_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (req_id, run_id, command, justification, RequestStatus.PENDING.value,
             ticket_id, _now(), expires_at),
        )
        self._conn.commit()
        return req_id

    def _row_to_request(self, row: sqlite3.Row) -> PendingRequest:
        return PendingRequest(
            id=row["id"], run_id=row["run_id"], command=row["command"],
            justification=row["justification"], status=RequestStatus(row["status"]),
            ticket_id=row["ticket_id"], created_at=row["created_at"],
            expires_at=row["expires_at"], resolved_at=row["resolved_at"],
        )

    def find_request(self, run_id: str, command: str) -> PendingRequest | None:
        row = self._conn.execute(
            "SELECT * FROM pending_requests WHERE run_id = ? AND command = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (run_id, command),
        ).fetchone()
        return self._row_to_request(row) if row else None

    def set_request_status(self, request_id: str, status: RequestStatus) -> None:
        resolved = None if status is RequestStatus.PENDING else _now()
        self._conn.execute(
            "UPDATE pending_requests SET status = ?, resolved_at = ? WHERE id = ?",
            (status.value, resolved, request_id),
        )
        self._conn.commit()

    def list_pending_requests(self) -> list[PendingRequest]:
        rows = self._conn.execute(
            "SELECT * FROM pending_requests WHERE status = ? ORDER BY created_at",
            (RequestStatus.PENDING.value,),
        ).fetchall()
        return [self._row_to_request(r) for r in rows]

    # ---- audit --------------------------------------------------------
    def add_audit(self, run_id: str, event: str, command: str | None,
                  decision: str | None, detail: str | None) -> None:
        self._conn.execute(
            "INSERT INTO audit_log (run_id, ts, event, command, decision, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, _now(), event, command, decision, detail),
        )
        self._conn.commit()

    def list_audit(self, run_id: str) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            AuditEntry(
                id=r["id"], run_id=r["run_id"], ts=r["ts"], event=r["event"],
                command=r["command"], decision=r["decision"], detail=r["detail"],
            )
            for r in rows
        ]
