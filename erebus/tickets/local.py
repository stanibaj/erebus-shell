"""Local SQLite-backed ticket provider for dev/test and self-hosted use.

Implements the async TicketProvider contract (create/poll) and adds synchronous
human-side helpers (approve/deny/list_pending/get) that a CLI or HTTP endpoint
drives. A real provider like Zoho replaces this entirely; its "human side" is
the Zoho web UI rather than these helpers.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from erebus.state.models import RequestStatus
from erebus.tickets.base import TicketRequest, TicketStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Ticket:
    id: str
    run_id: str
    command: str
    justification: str
    decision: RequestStatus
    note: str | None
    created_at: str
    resolved_at: str | None


class LocalTicketProvider:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")

    def init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                command TEXT NOT NULL,
                justification TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            """
        )
        self._conn.commit()

    # ---- TicketProvider contract (async) -----------------------------
    async def create(self, req: TicketRequest) -> str:
        ticket_id = "T-" + uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO tickets "
            "(id, run_id, command, justification, decision, note, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
            (ticket_id, req.run_id, req.command, req.justification,
             RequestStatus.PENDING.value, _now()),
        )
        self._conn.commit()
        return ticket_id

    async def poll(self, ticket_id: str) -> TicketStatus:
        t = self.get(ticket_id)
        return TicketStatus(ticket_id=t.id, decision=t.decision, note=t.note)

    # ---- human-side helpers (sync) -----------------------------------
    def get(self, ticket_id: str) -> Ticket:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown ticket: {ticket_id}")
        return self._row_to_ticket(row)

    def approve(self, ticket_id: str, note: str | None = None) -> None:
        self._resolve(ticket_id, RequestStatus.APPROVED, note)

    def deny(self, ticket_id: str, note: str | None = None) -> None:
        self._resolve(ticket_id, RequestStatus.DENIED, note)

    def list_pending(self) -> list[Ticket]:
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE decision = ? ORDER BY created_at",
            (RequestStatus.PENDING.value,),
        ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    # ---- internals ----------------------------------------------------
    def _resolve(self, ticket_id: str, decision: RequestStatus, note: str | None) -> None:
        cur = self._conn.execute(
            "UPDATE tickets SET decision = ?, note = ?, resolved_at = ? WHERE id = ?",
            (decision.value, note, _now(), ticket_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown ticket: {ticket_id}")

    def _row_to_ticket(self, row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row["id"], run_id=row["run_id"], command=row["command"],
            justification=row["justification"], decision=RequestStatus(row["decision"]),
            note=row["note"], created_at=row["created_at"], resolved_at=row["resolved_at"],
        )
