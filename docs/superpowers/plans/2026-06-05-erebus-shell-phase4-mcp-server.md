# Erebus Shell — Phase 4: MCP Server (run_command chokepoint) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Read `2026-06-05-erebus-shell-overview.md` for the `run_command` algorithm and the locked interfaces. Phases 1–3 are merged. The `mcp>=1.2` dependency is added in `pyproject.toml` and the image is rebuilt.

**Goal:** Implement the single chokepoint — a `CommandGate` that wires `parse_command` + `PolicyEngine` + `Executor` + `TicketProvider` + `Store` into the full allow/block/escalate/resume algorithm — and expose it as a `run_command` MCP tool via `FastMCP`.

**Architecture:** Split the heart from the transport. `erebus/mcp/gate.py` holds `CommandGate.handle(command, reason)` — pure orchestration over injected dependencies, fully unit-testable with the real `LocalExecutor`/`LocalTicketProvider`/`Store`. `erebus/mcp/server.py` is thin: `build_mcp(gate)` registers the `run_command` tool on a `FastMCP` instance, `build_gate_from_env()` constructs a gate from env vars + the YAML policy, and `main()` runs the stdio server (used by the Phase 5 supervisor). The gate reads the **store's** pending-request status (not the ticket provider) on retry — the supervisor is what reconciles ticket decision → store status. The gate only ever *creates* tickets.

**Verified `mcp` API (probed in-container):** `FastMCP('name')`; `@mcp.tool()` registers; `await mcp.list_tools()` → `list[MCPTool]` (has `.name`); `await mcp.call_tool(name, args)` → `(list[TextContent], {"result": <return>})`.

**Tech Stack:** `mcp` (FastMCP), Phases 1–3 modules, `pytest`/`pytest-asyncio`. All runs in Docker.

---

### Task 1: CommandGate — the run_command algorithm

**Files:**
- Create: `erebus/mcp/__init__.py` (empty)
- Create: `erebus/mcp/gate.py`
- Test: `tests/mcp/__init__.py` (empty), `tests/mcp/test_gate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/__init__.py` (empty). Create `tests/mcp/test_gate.py`:

```python
import pytest

from erebus.mcp.gate import CommandGate
from erebus.policy.engine import PolicyEngine
from erebus.policy.models import Policy, AllowRule
from erebus.executor.local import LocalExecutor
from erebus.tickets.local import LocalTicketProvider
from erebus.state.store import Store
from erebus.state.models import RunStatus, RequestStatus


@pytest.fixture()
def wired(tmp_path):
    store = Store(str(tmp_path / "state.db"))
    store.init_schema()
    tickets = LocalTicketProvider(str(tmp_path / "tickets.db"))
    tickets.init_schema()
    run_id = store.create_run(agent="test", task="t")
    # echo and ls are allowed; everything else (e.g. whoami) is blocked.
    engine = PolicyEngine(Policy(allow=[AllowRule(binary="echo"), AllowRule(binary="ls")]))
    gate = CommandGate(
        run_id=run_id, engine=engine, executor=LocalExecutor(),
        tickets=tickets, store=store, ttl_hours=24,
    )
    return gate, store, tickets, run_id


async def test_allow_executes_and_returns_output(wired):
    gate, store, tickets, run_id = wired
    out = await gate.handle("echo hello")
    assert "hello" in out
    assert "exit_code: 0" in out
    events = [e.event for e in store.list_audit(run_id)]
    assert "executed" in events


async def test_operator_blocked_no_ticket(wired):
    gate, store, tickets, run_id = wired
    out = await gate.handle("echo hi && echo pwned")
    assert "operator" in out.lower()
    assert "&&" in out
    # No ticket and no pending request created for a malformed/operator command.
    assert tickets.list_pending() == []
    assert store.find_request(run_id, "echo hi && echo pwned") is None


async def test_empty_command_returns_error(wired):
    gate, store, tickets, run_id = wired
    out = await gate.handle("   ")
    assert "error" in out.lower()
    assert tickets.list_pending() == []


async def test_block_first_attempt_creates_ticket_and_pends(wired):
    gate, store, tickets, run_id = wired
    out = await gate.handle("whoami", reason="need to know the user")
    assert "approval" in out.lower()
    pending = tickets.list_pending()
    assert len(pending) == 1
    assert pending[0].command == "whoami"
    assert pending[0].justification == "need to know the user"
    assert pending[0].id in out                      # ticket id surfaced to the agent
    req = store.find_request(run_id, "whoami")
    assert req is not None and req.status is RequestStatus.PENDING
    assert store.get_run(run_id).status is RunStatus.PENDING_APPROVAL
    assert "escalated" in [e.event for e in store.list_audit(run_id)]


async def test_block_second_attempt_while_pending_no_duplicate_ticket(wired):
    gate, store, tickets, run_id = wired
    await gate.handle("whoami", reason="r")
    out2 = await gate.handle("whoami", reason="r again")
    assert "approval" in out2.lower()
    assert len(tickets.list_pending()) == 1          # still only one ticket


async def test_block_empty_reason_uses_fallback_justification(wired):
    gate, store, tickets, run_id = wired
    await gate.handle("whoami")
    assert tickets.list_pending()[0].justification.startswith("(no rationale")


async def test_executes_after_store_request_approved(wired):
    # Simulate the supervisor having reconciled the ticket approval into the store.
    gate, store, tickets, run_id = wired
    await gate.handle("whoami")
    req = store.find_request(run_id, "whoami")
    store.set_request_status(req.id, RequestStatus.APPROVED)
    out = await gate.handle("whoami")
    assert "exit_code: 0" in out                     # actually ran whoami
    assert "executed_after_approval" in [e.event for e in store.list_audit(run_id)]


async def test_denied_request_returns_denial_and_does_not_execute(wired):
    gate, store, tickets, run_id = wired
    await gate.handle("whoami")
    req = store.find_request(run_id, "whoami")
    store.set_request_status(req.id, RequestStatus.DENIED)
    out = await gate.handle("whoami")
    assert "denied" in out.lower()
    assert "exit_code" not in out                     # did not execute


async def test_allowed_but_missing_binary_returns_error_not_exception(wired, tmp_path):
    # Allow a binary that does not exist; gate must not raise out of handle().
    gate, store, tickets, run_id = wired
    gate._engine = PolicyEngine(Policy(allow=[AllowRule(binary="erebus_missing_bin_xyz")]))
    out = await gate.handle("erebus_missing_bin_xyz")
    assert "error" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm test pytest tests/mcp/test_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.mcp'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/mcp/__init__.py` (empty). Create `erebus/mcp/gate.py`:

```python
"""CommandGate: the single chokepoint algorithm behind the run_command tool.

Orchestrates parse -> policy -> {execute | escalate} over injected dependencies.
On a blocked command it reads the STORE's pending-request status (the supervisor
reconciles ticket decisions into the store); it only ever *creates* tickets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from erebus.executor.base import Executor, ExecResult, ExecutionTimeout
from erebus.policy.engine import DecisionType, PolicyEngine
from erebus.policy.parsing import CommandParseError, parse_command
from erebus.state.models import RequestStatus, RunStatus
from erebus.state.store import Store
from erebus.tickets.base import TicketProvider, TicketRequest

_DENIAL_MSG = (
    "This command was denied by a human. Do not retry it. "
    "Use an allowed alternative or stop."
)
_FALLBACK_JUSTIFICATION = "(no rationale provided; see run transcript)"


class CommandGate:
    def __init__(
        self,
        *,
        run_id: str,
        engine: PolicyEngine,
        executor: Executor,
        tickets: TicketProvider,
        store: Store,
        ttl_hours: float = 24.0,
    ) -> None:
        self._run_id = run_id
        self._engine = engine
        self._executor = executor
        self._tickets = tickets
        self._store = store
        self._ttl_hours = ttl_hours

    async def handle(self, command: str, reason: str = "") -> str:
        run_id = self._run_id

        # 1. Parse + reject shell operators (usage error, not an escalation).
        try:
            parsed = parse_command(command)
        except CommandParseError as exc:
            self._store.add_audit(run_id, "parse_error", command, None, str(exc))
            return f"Error: {exc}. Issue a single, valid command."

        if parsed.contains_operators:
            ops = ", ".join(parsed.operators_found)
            self._store.add_audit(run_id, "operator_blocked", command, "block", ops)
            return (
                f"Shell operators ({ops}) are not supported. Issue a single command, "
                "or if a pipeline is essential, explain why and request approval."
            )

        # 2. Policy decision.
        decision = self._engine.evaluate(parsed.argv)
        if decision.type is DecisionType.ALLOW:
            return await self._execute(parsed.argv, command, event="executed")

        # 3. Blocked — has this exact command already been routed for this run?
        existing = self._store.find_request(run_id, command)
        if existing is not None:
            if existing.status is RequestStatus.APPROVED:
                return await self._execute(
                    parsed.argv, command, event="executed_after_approval"
                )
            if existing.status in (RequestStatus.DENIED, RequestStatus.EXPIRED):
                return _DENIAL_MSG
            return self._pending_msg(existing.ticket_id)

        # 4. First attempt -> create ticket on first attempt.
        justification = reason.strip() or _FALLBACK_JUSTIFICATION
        ticket_id = await self._tickets.create(
            TicketRequest(run_id=run_id, command=command, justification=justification)
        )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=self._ttl_hours)
        ).isoformat()
        self._store.create_pending_request(
            run_id=run_id, command=command, justification=justification,
            ticket_id=ticket_id, expires_at=expires_at,
        )
        self._store.set_run_status(run_id, RunStatus.PENDING_APPROVAL)
        self._store.add_audit(run_id, "escalated", command, "block", f"ticket {ticket_id}")
        return self._pending_msg(ticket_id)

    async def _execute(self, argv: list[str], command: str, *, event: str) -> str:
        try:
            result = await self._executor.execute(argv)
        except (FileNotFoundError, ExecutionTimeout, ValueError) as exc:
            self._store.add_audit(self._run_id, "execution_error", command, "allow", str(exc))
            return f"Error executing command: {exc}"
        self._store.add_audit(
            self._run_id, event, command, "allow", f"exit={result.exit_code}"
        )
        return self._format(result)

    def _pending_msg(self, ticket_id: str) -> str:
        return (
            "This command is not on the allowlist. A request for human approval has "
            f"been created (ticket {ticket_id}). This run will pause; it resumes "
            "automatically if approved. Stop now."
        )

    @staticmethod
    def _format(result: ExecResult) -> str:
        parts = [f"exit_code: {result.exit_code}"]
        if result.stdout:
            parts.append("stdout:\n" + result.stdout.rstrip("\n"))
        if result.stderr:
            parts.append("stderr:\n" + result.stderr.rstrip("\n"))
        return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose run --rm test pytest tests/mcp/test_gate.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add erebus/mcp/__init__.py erebus/mcp/gate.py tests/mcp/__init__.py tests/mcp/test_gate.py
git commit -m "feat(mcp): CommandGate chokepoint algorithm (allow/block/escalate/resume)"
```

---

### Task 2: MCP server wiring (FastMCP) + env factory

**Files:**
- Create: `erebus/mcp/server.py`
- Test: `tests/mcp/test_server.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_server.py`:

```python
import pytest

from erebus.mcp.server import build_mcp, build_gate_from_env
from erebus.mcp.gate import CommandGate
from erebus.policy.engine import PolicyEngine
from erebus.policy.models import Policy, AllowRule
from erebus.executor.local import LocalExecutor
from erebus.tickets.local import LocalTicketProvider
from erebus.state.store import Store
from erebus.state.models import RequestStatus


def _gate(tmp_path):
    store = Store(str(tmp_path / "state.db"))
    store.init_schema()
    tickets = LocalTicketProvider(str(tmp_path / "tickets.db"))
    tickets.init_schema()
    run_id = store.create_run(agent="test", task="t")
    engine = PolicyEngine(Policy(allow=[AllowRule(binary="echo")]))
    gate = CommandGate(run_id=run_id, engine=engine, executor=LocalExecutor(),
                       tickets=tickets, store=store)
    return gate, store, tickets, run_id


async def test_run_command_tool_registered(tmp_path):
    gate, *_ = _gate(tmp_path)
    mcp = build_mcp(gate)
    names = [t.name for t in await mcp.list_tools()]
    assert "run_command" in names


async def test_call_tool_allows_and_returns_output(tmp_path):
    gate, *_ = _gate(tmp_path)
    mcp = build_mcp(gate)
    content, structured = await mcp.call_tool("run_command", {"command": "echo hi"})
    assert "hi" in structured["result"]
    assert "hi" in content[0].text


async def test_call_tool_blocks_and_creates_ticket(tmp_path):
    gate, store, tickets, run_id = _gate(tmp_path)
    mcp = build_mcp(gate)
    content, structured = await mcp.call_tool(
        "run_command", {"command": "whoami", "reason": "diagnostics"}
    )
    assert "approval" in structured["result"].lower()
    assert len(tickets.list_pending()) == 1
    assert store.find_request(run_id, "whoami").status is RequestStatus.PENDING


def test_build_gate_from_env(tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("allow:\n  - binary: echo\n")
    monkeypatch.setenv("EREBUS_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("EREBUS_TICKETS_DB", str(tmp_path / "tickets.db"))
    monkeypatch.setenv("EREBUS_POLICY_PATH", str(policy_file))
    # run_id must exist in the store; create it first via a Store on the same db.
    store = Store(str(tmp_path / "state.db"))
    store.init_schema()
    run_id = store.create_run(agent="test", task="t")
    monkeypatch.setenv("EREBUS_RUN_ID", run_id)

    gate = build_gate_from_env()
    assert isinstance(gate, CommandGate)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm test pytest tests/mcp/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erebus.mcp.server'`.

- [ ] **Step 3: Write the implementation**

Create `erebus/mcp/server.py`:

```python
"""FastMCP wiring for the run_command chokepoint.

`build_mcp(gate)` exposes exactly one tool, `run_command`. `build_gate_from_env`
constructs a CommandGate from env vars + the YAML policy (used by the Phase 5
supervisor, which spawns this server over stdio with EREBUS_* set). `main()`
runs the stdio server.
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from erebus.executor.local import LocalExecutor
from erebus.mcp.gate import CommandGate
from erebus.policy.engine import PolicyEngine
from erebus.policy.models import load_policy_from_yaml
from erebus.state.store import Store
from erebus.tickets.local import LocalTicketProvider


def build_mcp(gate: CommandGate) -> FastMCP:
    mcp = FastMCP("erebus")

    @mcp.tool()
    async def run_command(command: str, reason: str = "") -> str:
        """Run a single shell command through the Erebus allowlist gate.

        If the command is not allowlisted, an approval ticket is created and the
        run pauses until a human approves. `reason` is your rationale for needing
        a not-allowed command; it is shown to the human approver.
        """
        return await gate.handle(command, reason)

    return mcp


def build_gate_from_env() -> CommandGate:
    run_id = os.environ["EREBUS_RUN_ID"]
    db_path = os.environ["EREBUS_DB_PATH"]
    tickets_db = os.environ["EREBUS_TICKETS_DB"]
    policy_path = os.environ["EREBUS_POLICY_PATH"]
    ttl_hours = float(os.environ.get("EREBUS_TTL_HOURS", "24"))

    store = Store(db_path)
    store.init_schema()
    tickets = LocalTicketProvider(tickets_db)
    tickets.init_schema()
    engine = PolicyEngine(load_policy_from_yaml(policy_path))
    return CommandGate(
        run_id=run_id, engine=engine, executor=LocalExecutor(),
        tickets=tickets, store=store, ttl_hours=ttl_hours,
    )


def main() -> None:  # pragma: no cover - exercised via the supervisor in Phase 5
    build_mcp(build_gate_from_env()).run()


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose run --rm test pytest tests/mcp/test_server.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add erebus/mcp/server.py tests/mcp/test_server.py
git commit -m "feat(mcp): FastMCP run_command tool + env-based gate factory"
```

---

### Task 3: Phase 4 gate

- [ ] **Step 1: Run the full suite**

Run: `docker compose run --rm test pytest -q`
Expected: PASS — Phases 1–3 (56) + Phase 4 (9 + 4 = 13) = 69 tests.

- [ ] **Step 2: Commit the dependency change** (if not already committed with the branch)

```bash
git add pyproject.toml
git commit -m "build: add mcp SDK dependency" --allow-empty
```

---

## Self-Review

- **Spec coverage:** Implements the overview's `run_command` algorithm exactly — operator block (no ticket), allow→execute, block→{approved→execute / denied→message / pending→message}, first-block→create ticket + pending request + PENDING_APPROVAL + audit. Reads store status on retry (supervisor reconciles ticket→store, per overview). Exposes one generic `run_command` tool (Decision #5). All covered by Tasks 1–2.
- **Placeholder scan:** none — complete code/tests; `main()`/`__main__` marked `pragma: no cover` (real stdio loop exercised in Phase 5).
- **Type consistency:** `CommandGate(run_id, engine, executor, tickets, store, ttl_hours)` uses Phase 1–3 types unchanged: `PolicyEngine.evaluate`→`Decision`/`DecisionType`, `Executor.execute`→`ExecResult`/`ExecutionTimeout`, `TicketProvider.create(TicketRequest)`→`str`, `Store.find_request/create_pending_request/set_run_status/add_audit`, `RequestStatus`/`RunStatus`. `call_tool` return `(content, {"result": ...})` matches the probed `mcp` API.

## Next

After Phase 4 is green, request the **Phase 5 (supervisor)** plan — `ClaudeCodeAdapter`, the orchestrator lifecycle state machine, the FastAPI `POST /runs`/`GET /runs/{id}`/approve-deny surface, and the CLI — wiring an end-to-end run with the local ticket provider and a stub agent in tests.
