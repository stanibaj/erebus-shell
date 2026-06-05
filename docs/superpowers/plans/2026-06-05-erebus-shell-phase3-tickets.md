# Erebus Shell — Phase 3: Ticket Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Read `2026-06-05-erebus-shell-overview.md` for the locked `TicketProvider`/`TicketRequest`/`TicketStatus` interface. Phases 1–2 are merged to `main`.

**Goal:** Build the `TicketProvider` interface and a `LocalTicketProvider` (SQLite-backed) so the full gate→ticket→poll→resume loop is testable with zero external dependencies. The human approve/deny side is plain method calls a CLI/HTTP endpoint will drive in Phase 5.

**Architecture:** `tickets/base.py` defines the `TicketProvider` Protocol plus the `TicketRequest` and `TicketStatus` dataclasses (decision reuses Phase 1's `RequestStatus` enum). `tickets/local.py` implements `LocalTicketProvider`, which owns a `tickets` table in a SQLite file: `create`/`poll` are the async provider methods (matching the Zoho-style async contract); `approve`/`deny`/`list_pending`/`get` are the synchronous human-side helpers. Keeping the human-side helpers separate from the `TicketProvider` Protocol is intentional — Zoho's "human side" is its own web UI, not our code.

**Tech Stack:** stdlib `sqlite3`, `pytest` + `pytest-asyncio` (auto mode). All runs in Docker: `docker compose run --rm test pytest ...`.

---

### Task 1: TicketProvider interface + request/status dataclasses

**Files:**
- Create: `erebus/tickets/__init__.py` (empty)
- Create: `erebus/tickets/base.py`
- Test: `tests/tickets/__init__.py` (empty), `tests/tickets/test_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tickets/__init__.py` (empty). Create `tests/tickets/test_base.py`:

```python
from erebus.tickets.base import TicketRequest, TicketStatus, TicketProvider
from erebus.state.models import RequestStatus


def test_ticket_request_fields():
    req = TicketRequest(run_id="r1", command="systemctl restart nginx", justification="down")
    assert req.run_id == "r1"
    assert req.command == "systemctl restart nginx"
    assert req.justification == "down"


def test_ticket_status_fields_default_note_none():
    s = TicketStatus(ticket_id="T-1", decision=RequestStatus.PENDING)
    assert s.ticket_id == "T-1"
    assert s.decision is RequestStatus.PENDING
    assert s.note is None


def test_ticket_provider_is_protocol():
    assert hasattr(TicketProvider, "create")
    assert hasattr(TicketProvider, "poll")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm test pytest tests/tickets/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.tickets'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/tickets/__init__.py` (empty). Create `erebus/tickets/base.py`:

```python
"""TicketProvider interface: create an approval ticket and poll its decision."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from erebus.state.models import RequestStatus


@dataclass(frozen=True)
class TicketRequest:
    run_id: str
    command: str
    justification: str


@dataclass(frozen=True)
class TicketStatus:
    ticket_id: str
    decision: RequestStatus          # PENDING / APPROVED / DENIED
    note: str | None = None


class TicketProvider(Protocol):
    async def create(self, req: TicketRequest) -> str: ...   # returns ticket_id
    async def poll(self, ticket_id: str) -> TicketStatus: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm test pytest tests/tickets/test_base.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add erebus/tickets/__init__.py erebus/tickets/base.py tests/tickets/__init__.py tests/tickets/test_base.py
git commit -m "feat(tickets): TicketProvider protocol + request/status types"
```

---

### Task 2: LocalTicketProvider (SQLite-backed approve/deny)

**Files:**
- Create: `erebus/tickets/local.py`
- Test: `tests/tickets/test_local.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/tickets/test_local.py`:

```python
import pytest
from erebus.tickets.local import LocalTicketProvider, Ticket
from erebus.tickets.base import TicketRequest
from erebus.state.models import RequestStatus


@pytest.fixture()
def provider(tmp_path):
    p = LocalTicketProvider(str(tmp_path / "tickets.db"))
    p.init_schema()
    return p


def _req(cmd="systemctl restart nginx"):
    return TicketRequest(run_id="r1", command=cmd, justification="service is down")


async def test_create_starts_pending(provider):
    tid = await provider.create(_req())
    assert isinstance(tid, str) and tid
    status = await provider.poll(tid)
    assert status.ticket_id == tid
    assert status.decision is RequestStatus.PENDING
    assert status.note is None


async def test_approve_then_poll(provider):
    tid = await provider.create(_req())
    provider.approve(tid, note="looks fine")
    status = await provider.poll(tid)
    assert status.decision is RequestStatus.APPROVED
    assert status.note == "looks fine"


async def test_deny_then_poll(provider):
    tid = await provider.create(_req())
    provider.deny(tid, note="too risky")
    status = await provider.poll(tid)
    assert status.decision is RequestStatus.DENIED
    assert status.note == "too risky"


async def test_poll_unknown_ticket_raises(provider):
    with pytest.raises(KeyError):
        await provider.poll("does-not-exist")


async def test_list_pending_returns_unresolved_with_context(provider):
    tid1 = await provider.create(_req("cmd-a"))
    tid2 = await provider.create(_req("cmd-b"))
    provider.approve(tid1)
    pending = provider.list_pending()
    assert len(pending) == 1
    t = pending[0]
    assert isinstance(t, Ticket)
    assert t.id == tid2
    assert t.command == "cmd-b"
    assert t.justification == "service is down"
    assert t.run_id == "r1"


async def test_get_returns_full_ticket(provider):
    tid = await provider.create(_req("cmd-x"))
    t = provider.get(tid)
    assert t.id == tid
    assert t.command == "cmd-x"
    assert t.decision is RequestStatus.PENDING


async def test_persists_across_reopen(tmp_path):
    db = str(tmp_path / "tickets.db")
    p1 = LocalTicketProvider(db)
    p1.init_schema()
    tid = await p1.create(_req("persist"))
    p2 = LocalTicketProvider(db)            # simulate restart
    status = await p2.poll(tid)
    assert status.decision is RequestStatus.PENDING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm test pytest tests/tickets/test_local.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.tickets.local'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/tickets/local.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose run --rm test pytest tests/tickets/test_local.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add erebus/tickets/local.py tests/tickets/test_local.py
git commit -m "feat(tickets): LocalTicketProvider with SQLite approve/deny"
```

---

### Task 3: Phase 3 gate

- [ ] **Step 1: Run the full suite**

Run: `docker compose run --rm test pytest -q`
Expected: PASS — Phase 1 (36) + Phase 2 (10) + Phase 3 (3 + 7 = 10) = 56 tests.

---

## Self-Review

- **Spec coverage:** Phase 3 scope = `TicketProvider` interface + `LocalTicketProvider` (create/poll + human approve/deny), per the overview's interfaces and Decision #12 (local reference provider first). Covered by Tasks 1–2. `list_pending`/`get` are added because Phase 5's CLI/HTTP approval surface needs to show pending tickets with their command + justification.
- **Placeholder scan:** none — complete code and tests, exact commands.
- **Type consistency:** `TicketRequest(run_id, command, justification)`, `TicketStatus(ticket_id, decision, note=None)`, and `TicketProvider.create/poll` match the overview's locked `tickets/base.py`. `decision` reuses `RequestStatus` from `state/models.py` (consistent with the store). `LocalTicketProvider.create` returns `str` (ticket_id), satisfying the Protocol.

## Next

After Phase 3 is green, request the **Phase 4 (MCP server)** plan — the `run_command` tool that wires `PolicyEngine` + `parse_command` + `LocalExecutor` + `TicketProvider` + `Store` into the single chokepoint, tested via an in-process MCP client.
