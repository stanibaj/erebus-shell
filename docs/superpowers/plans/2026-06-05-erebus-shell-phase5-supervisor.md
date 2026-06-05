# Erebus Shell — Phase 5: Supervisor (adapter + orchestrator + HTTP + CLI) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Read the overview for the lifecycle state machine and locked types. Phases 1–4 merged. Deps added: `fastapi`, `uvicorn`, `httpx`; image rebuilt. All runs in Docker.

**Goal:** Assemble the running system: an `AgentAdapter` contract + a `StubAgentAdapter` (for deterministic CI) + a `ClaudeCodeAdapter` (real, pure parts tested), an `Orchestrator` implementing the launch→pending→poll→resume/deny/expire state machine, a FastAPI service (`POST /runs`, `GET /runs/{id}`, ticket approve/deny/list), and a thin `erebus` CLI.

**Architecture:** The `Orchestrator` depends only on the `AgentAdapter` Protocol, the `Store`, and a `TicketProvider`, so the whole loop is exercised end-to-end in tests via `StubAgentAdapter` — which drives the **real** `CommandGate` (sharing the orchestrator's store + tickets), proving pause→approve→resume without a real agent. `ClaudeCodeAdapter` keeps its pure parts (`render_launch`, `parse_outcome`) unit-tested; the subprocess glue is `pragma: no cover` (validated manually with the real `claude` CLI). The FastAPI service wires an orchestrator + provider; approving a ticket over HTTP triggers `poll_and_resume`.

**Tech Stack:** Phases 1–4 modules, `fastapi`/`fastapi.testclient`, `httpx` (CLI), `argparse`.

---

### Task 1: AgentAdapter contract + StubAgentAdapter

**Files:** Create `erebus/agents/__init__.py` (empty), `erebus/agents/base.py`, `erebus/agents/stub.py`; Test `tests/agents/__init__.py` (empty), `tests/agents/test_stub.py`.

- [ ] **Step 1: failing test** — `tests/agents/test_stub.py`:

```python
import pytest
from erebus.agents.base import RunContext, RunOutcome
from erebus.agents.stub import StubAgentAdapter
from erebus.mcp.gate import CommandGate
from erebus.policy.engine import PolicyEngine
from erebus.policy.models import Policy, AllowRule
from erebus.executor.local import LocalExecutor
from erebus.tickets.local import LocalTicketProvider
from erebus.state.store import Store
from erebus.state.models import RequestStatus


@pytest.fixture()
def env(tmp_path):
    store = Store(str(tmp_path / "s.db")); store.init_schema()
    tickets = LocalTicketProvider(str(tmp_path / "t.db")); tickets.init_schema()
    engine = PolicyEngine(Policy(allow=[AllowRule(binary="echo")]))

    def gate_factory(run_id):
        return CommandGate(run_id=run_id, engine=engine, executor=LocalExecutor(),
                           tickets=tickets, store=store)

    return store, tickets, gate_factory


async def test_stub_runs_until_block(env):
    store, tickets, gf = env
    run_id = store.create_run(agent="stub", task="t")
    stub = StubAgentAdapter(script=["echo hi", "whoami", "echo bye"], gate_factory=gf)
    outcome = await stub.run(RunContext(run_id=run_id, task="t", allowlist_text="",
                                        resume=False, session_id=None, message=None))
    assert isinstance(outcome, RunOutcome)
    assert outcome.session_id
    # echo hi ran; whoami blocked & pending; echo bye not reached yet.
    assert store.find_request(run_id, "whoami").status is RequestStatus.PENDING
    assert len(tickets.list_pending()) == 1


async def test_stub_resumes_after_approval(env):
    store, tickets, gf = env
    run_id = store.create_run(agent="stub", task="t")
    stub = StubAgentAdapter(script=["whoami", "echo bye"], gate_factory=gf)
    await stub.run(RunContext(run_id=run_id, task="t", allowlist_text="",
                              resume=False, session_id=None, message=None))
    req = store.find_request(run_id, "whoami")
    store.set_request_status(req.id, RequestStatus.APPROVED)        # supervisor reconciled
    await stub.run(RunContext(run_id=run_id, task="t", allowlist_text="",
                              resume=True, session_id="s", message="approved"))
    # whoami executed after approval, then echo bye executed; no pending left.
    events = [e.event for e in store.list_audit(run_id)]
    assert "executed_after_approval" in events
    assert tickets.list_pending() == []
```

- [ ] **Step 2: run, expect fail** — `docker compose run --rm test pytest tests/agents/test_stub.py -v` → `ModuleNotFoundError: erebus.agents`.

- [ ] **Step 3: implement** — `erebus/agents/base.py`:

```python
"""AgentAdapter contract. An adapter launches/resumes an agent and reports outcome."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunContext:
    run_id: str
    task: str
    allowlist_text: str
    resume: bool
    session_id: str | None
    message: str | None


@dataclass(frozen=True)
class RunOutcome:
    session_id: str | None
    exit_code: int
    raw_output: str


class AgentAdapter(Protocol):
    name: str
    async def run(self, ctx: RunContext) -> RunOutcome: ...
```

`erebus/agents/stub.py`:

```python
"""Deterministic stub agent for tests. Drives the REAL CommandGate so the whole
supervisor loop is exercised without a real LLM agent. Keeps a cursor across
launch/resume calls so a blocked command is re-issued on resume.
"""
from __future__ import annotations

from typing import Callable

from erebus.agents.base import RunContext, RunOutcome
from erebus.mcp.gate import CommandGate


class StubAgentAdapter:
    name = "stub"

    def __init__(self, script: list[str], gate_factory: Callable[[str], CommandGate]) -> None:
        self._script = script
        self._gate_factory = gate_factory
        self._cursor = 0

    async def run(self, ctx: RunContext) -> RunOutcome:
        gate = self._gate_factory(ctx.run_id)
        outputs: list[str] = []
        while self._cursor < len(self._script):
            cmd = self._script[self._cursor]
            res = await gate.handle(cmd)
            outputs.append(res)
            if "approval has" in res and "ticket" in res.lower():
                # Blocked & pending: stop WITHOUT advancing so resume re-issues it.
                break
            if res.startswith("This command was denied"):
                # Denied on resume: skip it and continue.
                self._cursor += 1
                continue
            self._cursor += 1
        return RunOutcome(
            session_id=f"stub-{ctx.run_id}", exit_code=0, raw_output="\n".join(outputs)
        )
```

- [ ] **Step 4: run, expect pass** (2 tests). **Step 5: commit** `feat(agents): AgentAdapter contract + StubAgentAdapter`.

---

### Task 2: Orchestrator state machine (the supervisor heart)

**Files:** Create `erebus/supervisor/__init__.py` (empty), `erebus/supervisor/orchestrator.py`; Test `tests/supervisor/__init__.py` (empty), `tests/supervisor/test_orchestrator.py`.

- [ ] **Step 1: failing test** — `tests/supervisor/test_orchestrator.py`:

```python
import pytest
from erebus.supervisor.orchestrator import Orchestrator
from erebus.agents.stub import StubAgentAdapter
from erebus.mcp.gate import CommandGate
from erebus.policy.engine import PolicyEngine
from erebus.policy.models import Policy, AllowRule
from erebus.executor.local import LocalExecutor
from erebus.tickets.local import LocalTicketProvider
from erebus.state.store import Store
from erebus.state.models import RunStatus


def _build(tmp_path, script, on_deny="resume"):
    store = Store(str(tmp_path / "s.db")); store.init_schema()
    tickets = LocalTicketProvider(str(tmp_path / "t.db")); tickets.init_schema()
    engine = PolicyEngine(Policy(allow=[AllowRule(binary="echo")]))

    def gate_factory(run_id):
        return CommandGate(run_id=run_id, engine=engine, executor=LocalExecutor(),
                           tickets=tickets, store=store)

    stub = StubAgentAdapter(script=script, gate_factory=gate_factory)
    orch = Orchestrator(store=store, tickets=tickets, adapters={"stub": stub},
                        allowlist_text="echo", on_deny=on_deny)
    return orch, store, tickets


async def test_run_completes_when_all_allowed(tmp_path):
    orch, store, tickets = _build(tmp_path, ["echo a", "echo b"])
    run_id = await orch.start_run("t", "stub")
    assert store.get_run(run_id).status is RunStatus.COMPLETED


async def test_run_pends_on_block(tmp_path):
    orch, store, tickets = _build(tmp_path, ["echo a", "whoami", "echo b"])
    run_id = await orch.start_run("t", "stub")
    assert store.get_run(run_id).status is RunStatus.PENDING_APPROVAL
    assert len(tickets.list_pending()) == 1


async def test_approve_then_resume_completes(tmp_path):
    orch, store, tickets = _build(tmp_path, ["whoami", "echo b"])
    run_id = await orch.start_run("t", "stub")
    ticket_id = tickets.list_pending()[0].id
    tickets.approve(ticket_id)
    status = await orch.poll_and_resume(run_id)
    assert status == RunStatus.COMPLETED.value
    assert store.get_run(run_id).status is RunStatus.COMPLETED


async def test_deny_resume_mode_continues(tmp_path):
    orch, store, tickets = _build(tmp_path, ["whoami", "echo b"], on_deny="resume")
    run_id = await orch.start_run("t", "stub")
    tickets.deny(tickets.list_pending()[0].id)
    status = await orch.poll_and_resume(run_id)
    # denied command skipped, echo b ran -> completed
    assert status == RunStatus.COMPLETED.value


async def test_deny_abort_mode_marks_denied(tmp_path):
    orch, store, tickets = _build(tmp_path, ["whoami", "echo b"], on_deny="abort")
    run_id = await orch.start_run("t", "stub")
    tickets.deny(tickets.list_pending()[0].id)
    status = await orch.poll_and_resume(run_id)
    assert status == RunStatus.DENIED.value


async def test_expired_request_marks_expired(tmp_path):
    orch, store, tickets = _build(tmp_path, ["whoami"])
    run_id = await orch.start_run("t", "stub")
    # Force the pending request to be already expired.
    req = [r for r in store.list_pending_requests() if r.run_id == run_id][0]
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.execute("UPDATE pending_requests SET expires_at = ? WHERE id = ?",
                 ("2000-01-01T00:00:00+00:00", req.id))
    conn.commit(); conn.close()
    status = await orch.poll_and_resume(run_id)
    assert status == RunStatus.EXPIRED.value
```

- [ ] **Step 2: run, expect fail.** **Step 3: implement** — `erebus/supervisor/orchestrator.py`:

```python
"""Run lifecycle state machine: launch -> pending -> poll -> resume/deny/expire."""
from __future__ import annotations

from datetime import datetime, timezone

from erebus.agents.base import AgentAdapter, RunContext, RunOutcome
from erebus.state.models import PendingRequest, RequestStatus, RunStatus
from erebus.state.store import Store
from erebus.tickets.base import TicketProvider

_APPROVE_MSG = "The request was approved; the command will now succeed. Continue."
_DENY_MSG = "The request was denied. Do not retry; use an allowed alternative or stop."


class Orchestrator:
    def __init__(self, *, store: Store, tickets: TicketProvider,
                 adapters: dict[str, AgentAdapter], allowlist_text: str,
                 on_deny: str = "resume") -> None:
        self.store = store
        self.tickets = tickets
        self.adapters = adapters
        self.allowlist_text = allowlist_text
        self.on_deny = on_deny

    async def start_run(self, task: str, agent: str) -> str:
        run_id = self.store.create_run(agent=agent, task=task)
        ctx = RunContext(run_id=run_id, task=task, allowlist_text=self.allowlist_text,
                         resume=False, session_id=None, message=None)
        outcome = await self.adapters[agent].run(ctx)
        self._post_run(run_id, outcome)
        return run_id

    async def poll_and_resume(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        if run is None or run.status is not RunStatus.PENDING_APPROVAL:
            return run.status.value if run else "unknown"
        pending = self._pending_for_run(run_id)
        if pending is None:
            return run.status.value

        if datetime.now(timezone.utc) > datetime.fromisoformat(pending.expires_at):
            self.store.set_request_status(pending.id, RequestStatus.EXPIRED)
            self.store.set_run_status(run_id, RunStatus.EXPIRED)
            return RunStatus.EXPIRED.value

        status = await self.tickets.poll(pending.ticket_id)
        if status.decision is RequestStatus.PENDING:
            return RunStatus.PENDING_APPROVAL.value
        if status.decision is RequestStatus.APPROVED:
            self.store.set_request_status(pending.id, RequestStatus.APPROVED)
            self.store.set_run_status(run_id, RunStatus.RUNNING)
            await self._resume(run_id, _APPROVE_MSG)
        else:  # DENIED
            self.store.set_request_status(pending.id, RequestStatus.DENIED)
            if self.on_deny == "abort":
                self.store.set_run_status(run_id, RunStatus.DENIED)
                return RunStatus.DENIED.value
            self.store.set_run_status(run_id, RunStatus.RUNNING)
            await self._resume(run_id, _DENY_MSG)
        return self.store.get_run(run_id).status.value

    async def _resume(self, run_id: str, message: str) -> None:
        run = self.store.get_run(run_id)
        ctx = RunContext(run_id=run_id, task=run.task, allowlist_text=self.allowlist_text,
                         resume=True, session_id=run.session_id, message=message)
        outcome = await self.adapters[run.agent].run(ctx)
        self._post_run(run_id, outcome)

    def _post_run(self, run_id: str, outcome: RunOutcome) -> None:
        if outcome.session_id:
            self.store.set_run_session(run_id, outcome.session_id)
        if self._pending_for_run(run_id) is not None:
            self.store.set_run_status(run_id, RunStatus.PENDING_APPROVAL)
        elif outcome.exit_code == 0:
            self.store.set_run_status(run_id, RunStatus.COMPLETED)
        else:
            self.store.set_run_status(run_id, RunStatus.FAILED)

    def _pending_for_run(self, run_id: str) -> PendingRequest | None:
        for r in self.store.list_pending_requests():
            if r.run_id == run_id:
                return r
        return None
```

- [ ] **Step 4: run, expect pass** (6 tests). **Step 5: commit** `feat(supervisor): orchestrator launch/pending/poll/resume state machine`.

---

### Task 3: ClaudeCodeAdapter (pure parts tested; subprocess glue not)

**Files:** Create `erebus/agents/claude_code.py`; Test `tests/agents/test_claude_code.py`.

- [ ] **Step 1: failing test** — `tests/agents/test_claude_code.py`:

```python
import json
from erebus.agents.claude_code import ClaudeCodeAdapter
from erebus.agents.base import RunContext


def _ctx(resume=False, session_id=None):
    return RunContext(run_id="run-1", task="check disk", allowlist_text="echo, ls",
                      resume=resume, session_id=session_id, message="continue")


def test_render_launch_fresh_run(tmp_path):
    a = ClaudeCodeAdapter(policy_path="/cfg/policy.yaml", db_path="/d/s.db",
                          tickets_db="/d/t.db", max_turns=12)
    spec = a.render_launch(_ctx())
    assert spec.cmd[0] == "claude"
    assert "-p" in spec.cmd
    assert "check disk" in spec.cmd
    assert "--output-format" in spec.cmd and "json" in spec.cmd
    # native shell denied; only the erebus MCP tool allowed
    assert "Bash" in spec.cmd[spec.cmd.index("--disallowedTools") + 1]
    assert "mcp__erebus__run_command" in spec.cmd[spec.cmd.index("--allowedTools") + 1]
    assert "--resume" not in spec.cmd
    # env carries the per-run identity for the spawned MCP server
    assert spec.env["EREBUS_RUN_ID"] == "run-1"
    assert spec.env["EREBUS_DB_PATH"] == "/d/s.db"
    assert spec.env["EREBUS_POLICY_PATH"] == "/cfg/policy.yaml"


def test_render_launch_resume_includes_session(tmp_path):
    a = ClaudeCodeAdapter(policy_path="/cfg/policy.yaml", db_path="/d/s.db",
                          tickets_db="/d/t.db")
    spec = a.render_launch(_ctx(resume=True, session_id="sess-9"))
    assert "--resume" in spec.cmd
    assert "sess-9" in spec.cmd


def test_parse_outcome_extracts_session_and_result():
    a = ClaudeCodeAdapter(policy_path="p", db_path="d", tickets_db="t")
    raw = json.dumps({"session_id": "abc123", "result": "done", "is_error": False})
    out = a.parse_outcome(raw)
    assert out.session_id == "abc123"
    assert out.exit_code == 0
    assert "done" in out.raw_output


def test_parse_outcome_error_sets_nonzero_exit():
    a = ClaudeCodeAdapter(policy_path="p", db_path="d", tickets_db="t")
    raw = json.dumps({"session_id": "x", "result": "boom", "is_error": True})
    out = a.parse_outcome(raw)
    assert out.exit_code != 0


def test_parse_outcome_handles_non_json():
    a = ClaudeCodeAdapter(policy_path="p", db_path="d", tickets_db="t")
    out = a.parse_outcome("not json at all")
    assert out.session_id is None
    assert out.exit_code != 0
```

- [ ] **Step 2: run, expect fail.** **Step 3: implement** — `erebus/agents/claude_code.py`:

```python
"""Claude Code adapter. Pure config/parse helpers are unit-tested; the subprocess
glue (`run`) is validated manually with the real `claude` CLI + ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from erebus.agents.base import RunContext, RunOutcome

_MCP_TOOL = "mcp__erebus__run_command"
_SYSTEM_PROMPT_TMPL = (
    "You operate under Erebus, a gating shell. Your ONLY way to run commands is the "
    "`{tool}` MCP tool — native shell/Bash is disabled. These commands are allowed:\n"
    "{allowlist}\n"
    "Strongly prefer an allowed command. If you conclude no allowed alternative exists, "
    "you may call `{tool}` with the needed command and a clear `reason`; this creates a "
    "human approval ticket and pauses the run. Do not retry denied commands."
)


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(self, *, policy_path: str, db_path: str, tickets_db: str,
                 max_turns: int = 12, ttl_hours: float = 24.0) -> None:
        self._policy_path = policy_path
        self._db_path = db_path
        self._tickets_db = tickets_db
        self._max_turns = max_turns
        self._ttl_hours = ttl_hours

    # ---- pure helpers (tested) ---------------------------------------
    def _mcp_config(self, run_id: str) -> dict:
        return {
            "mcpServers": {
                "erebus": {
                    "command": "python",
                    "args": ["-m", "erebus.mcp.server"],
                    "env": self._env(run_id),
                }
            }
        }

    def _env(self, run_id: str) -> dict[str, str]:
        return {
            "EREBUS_RUN_ID": run_id,
            "EREBUS_DB_PATH": self._db_path,
            "EREBUS_TICKETS_DB": self._tickets_db,
            "EREBUS_POLICY_PATH": self._policy_path,
            "EREBUS_TTL_HOURS": str(self._ttl_hours),
        }

    def render_launch(self, ctx: RunContext) -> "LaunchSpec":
        from erebus.agents.base_spec import LaunchSpec  # local import to avoid cycle
        system_prompt = _SYSTEM_PROMPT_TMPL.format(tool=_MCP_TOOL, allowlist=ctx.allowlist_text)
        cmd = [
            "claude", "-p", ctx.message if ctx.resume else ctx.task,
            "--output-format", "json",
            "--allowedTools", _MCP_TOOL,
            "--disallowedTools", "Bash",
            "--append-system-prompt", system_prompt,
            "--max-turns", str(self._max_turns),
        ]
        if ctx.resume and ctx.session_id:
            cmd += ["--resume", ctx.session_id]
        return LaunchSpec(cmd=cmd, env=self._env(ctx.run_id),
                          mcp_config=self._mcp_config(ctx.run_id))

    def parse_outcome(self, raw_output: str) -> RunOutcome:
        try:
            data = json.loads(raw_output)
        except (json.JSONDecodeError, ValueError):
            return RunOutcome(session_id=None, exit_code=1, raw_output=raw_output)
        session_id = data.get("session_id")
        is_error = bool(data.get("is_error"))
        result = str(data.get("result", ""))
        return RunOutcome(session_id=session_id, exit_code=1 if is_error else 0,
                          raw_output=result)

    # ---- subprocess glue (manual / integration only) -----------------
    async def run(self, ctx: RunContext) -> RunOutcome:  # pragma: no cover
        spec = self.render_launch(ctx)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(spec.mcp_config, fh)
            mcp_path = fh.name
        cmd = spec.cmd + ["--mcp-config", mcp_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, env={**os.environ, **spec.env},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return self.parse_outcome(out.decode(errors="replace"))
```

Also create `erebus/agents/base_spec.py`:

```python
"""LaunchSpec lives in its own module to keep `base.py` import-light."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LaunchSpec:
    cmd: list[str]
    env: dict[str, str]
    mcp_config: dict = field(default_factory=dict)
```

- [ ] **Step 4: run, expect pass** (5 tests). **Step 5: commit** `feat(agents): ClaudeCodeAdapter render_launch + parse_outcome`.

---

### Task 4: FastAPI service

**Files:** Create `erebus/supervisor/service.py`; Test `tests/supervisor/test_service.py`.

- [ ] **Step 1: failing test** — `tests/supervisor/test_service.py`:

```python
import pytest
from fastapi.testclient import TestClient
from erebus.supervisor.service import create_app
from erebus.supervisor.orchestrator import Orchestrator
from erebus.agents.stub import StubAgentAdapter
from erebus.mcp.gate import CommandGate
from erebus.policy.engine import PolicyEngine
from erebus.policy.models import Policy, AllowRule
from erebus.executor.local import LocalExecutor
from erebus.tickets.local import LocalTicketProvider
from erebus.state.store import Store


@pytest.fixture()
def client(tmp_path):
    store = Store(str(tmp_path / "s.db")); store.init_schema()
    tickets = LocalTicketProvider(str(tmp_path / "t.db")); tickets.init_schema()
    engine = PolicyEngine(Policy(allow=[AllowRule(binary="echo")]))

    def gate_factory(run_id):
        return CommandGate(run_id=run_id, engine=engine, executor=LocalExecutor(),
                           tickets=tickets, store=store)

    stub = StubAgentAdapter(script=["whoami", "echo bye"], gate_factory=gate_factory)
    orch = Orchestrator(store=store, tickets=tickets, adapters={"stub": stub},
                        allowlist_text="echo")
    return TestClient(create_app(orchestrator=orch, tickets=tickets))


def test_create_run_pends_then_approve_completes(client):
    r = client.post("/runs", json={"task": "t", "agent": "stub"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "pending_approval"

    assert client.get(f"/runs/{run_id}").json()["status"] == "pending_approval"

    pend = client.get("/tickets/pending").json()
    assert len(pend) == 1
    ticket_id = pend[0]["id"]

    r2 = client.post(f"/tickets/{ticket_id}/approve", json={"note": "ok"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"
    assert client.get(f"/runs/{run_id}").json()["status"] == "completed"


def test_get_unknown_run_404(client):
    assert client.get("/runs/nope").status_code == 404
```

- [ ] **Step 2: run, expect fail.** **Step 3: implement** — `erebus/supervisor/service.py`:

```python
"""FastAPI surface: start/observe runs and approve/deny tickets (local provider)."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from erebus.supervisor.orchestrator import Orchestrator
from erebus.tickets.local import LocalTicketProvider


class CreateRun(BaseModel):
    task: str
    agent: str = "claude_code"


class Decision(BaseModel):
    note: str | None = None


def create_app(*, orchestrator: Orchestrator, tickets: LocalTicketProvider) -> FastAPI:
    app = FastAPI(title="Erebus Shell")

    def _run_json(run_id: str) -> dict:
        run = orchestrator.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"run_id": run.id, "agent": run.agent, "task": run.task,
                "status": run.status.value, "session_id": run.session_id}

    @app.post("/runs")
    async def create_run(body: CreateRun):
        run_id = await orchestrator.start_run(body.task, body.agent)
        return _run_json(run_id)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        return _run_json(run_id)

    @app.get("/tickets/pending")
    def pending():
        return [{"id": t.id, "run_id": t.run_id, "command": t.command,
                 "justification": t.justification} for t in tickets.list_pending()]

    @app.post("/tickets/{ticket_id}/approve")
    async def approve(ticket_id: str, body: Decision):
        try:
            t = tickets.get(ticket_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="ticket not found")
        tickets.approve(ticket_id, note=body.note)
        await orchestrator.poll_and_resume(t.run_id)
        return _run_json(t.run_id)

    @app.post("/tickets/{ticket_id}/deny")
    async def deny(ticket_id: str, body: Decision):
        try:
            t = tickets.get(ticket_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="ticket not found")
        tickets.deny(ticket_id, note=body.note)
        await orchestrator.poll_and_resume(t.run_id)
        return _run_json(t.run_id)

    return app
```

- [ ] **Step 4: run, expect pass** (2 tests). **Step 5: commit** `feat(supervisor): FastAPI runs + ticket approve/deny service`.

---

### Task 5: CLI (thin HTTP client)

**Files:** Create `erebus/cli.py`; Test `tests/test_cli.py`.

- [ ] **Step 1: failing test** — `tests/test_cli.py`:

```python
from erebus.cli import build_parser


def test_parser_run():
    ns = build_parser().parse_args(["run", "--task", "check disk", "--agent", "stub"])
    assert ns.command == "run"
    assert ns.task == "check disk"
    assert ns.agent == "stub"


def test_parser_approve():
    ns = build_parser().parse_args(["approve", "T-123", "--note", "ok"])
    assert ns.command == "approve"
    assert ns.ticket_id == "T-123"
    assert ns.note == "ok"


def test_parser_serve_defaults():
    ns = build_parser().parse_args(["serve"])
    assert ns.command == "serve"
    assert ns.host == "0.0.0.0"
    assert ns.port == 8080
```

- [ ] **Step 2: run, expect fail.** **Step 3: implement** — `erebus/cli.py`:

```python
"""Thin Erebus CLI. `run/pending/approve/deny` are HTTP clients of the service;
`serve` boots the FastAPI app from a YAML config.
"""
from __future__ import annotations

import argparse
import json
import os

import httpx


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="erebus")
    p.add_argument("--url", default=os.environ.get("EREBUS_URL", "http://localhost:8080"))
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a run")
    run.add_argument("--task", required=True)
    run.add_argument("--agent", default="claude_code")

    sub.add_parser("pending", help="list pending tickets")

    ap = sub.add_parser("approve", help="approve a ticket")
    ap.add_argument("ticket_id")
    ap.add_argument("--note", default=None)

    dn = sub.add_parser("deny", help="deny a ticket")
    dn.add_argument("ticket_id")
    dn.add_argument("--note", default=None)

    sv = sub.add_parser("serve", help="run the HTTP service")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--config", default=os.environ.get("EREBUS_CONFIG", "config/erebus.example.yaml"))
    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin glue
    ns = build_parser().parse_args(argv)
    if ns.command == "serve":
        import uvicorn
        from erebus.supervisor.bootstrap import build_app_from_config
        uvicorn.run(build_app_from_config(ns.config), host=ns.host, port=ns.port)
        return 0
    if ns.command == "run":
        r = httpx.post(f"{ns.url}/runs", json={"task": ns.task, "agent": ns.agent})
    elif ns.command == "pending":
        r = httpx.get(f"{ns.url}/tickets/pending")
    elif ns.command == "approve":
        r = httpx.post(f"{ns.url}/tickets/{ns.ticket_id}/approve", json={"note": ns.note})
    elif ns.command == "deny":
        r = httpx.post(f"{ns.url}/tickets/{ns.ticket_id}/deny", json={"note": ns.note})
    else:  # unreachable
        return 2
    print(json.dumps(r.json(), indent=2))
    return 0
```

Also create `erebus/supervisor/bootstrap.py` (used by `serve`; not unit-tested):

```python
"""Build a wired FastAPI app from a YAML config file."""
from __future__ import annotations

import yaml  # pragma: no cover

from erebus.agents.claude_code import ClaudeCodeAdapter  # pragma: no cover
from erebus.policy.models import load_policy_from_yaml  # pragma: no cover
from erebus.supervisor.orchestrator import Orchestrator  # pragma: no cover
from erebus.supervisor.service import create_app  # pragma: no cover
from erebus.tickets.local import LocalTicketProvider  # pragma: no cover


def build_app_from_config(config_path: str):  # pragma: no cover
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)
    db_path = cfg.get("db_path", "data/erebus.db")
    tickets_db = cfg.get("tickets_db", "data/tickets.db")
    from erebus.state.store import Store
    store = Store(db_path); store.init_schema()
    tickets = LocalTicketProvider(tickets_db); tickets.init_schema()
    policy = load_policy_from_yaml(config_path)
    allowlist_text = "\n".join(
        f"- {r.binary}" + (f" {r.args}" if r.args else "") for r in policy.allow
    )
    adapter = ClaudeCodeAdapter(policy_path=config_path, db_path=db_path,
                                tickets_db=tickets_db,
                                ttl_hours=float(cfg.get("approval_ttl_hours", 24)))
    orch = Orchestrator(store=store, tickets=tickets,
                        adapters={"claude_code": adapter}, allowlist_text=allowlist_text,
                        on_deny=cfg.get("on_deny", "resume"))
    return create_app(orchestrator=orch, tickets=tickets)
```

- [ ] **Step 4: run, expect pass** (3 tests). **Step 5: commit** `feat(cli): thin erebus CLI (run/pending/approve/deny/serve)`.

---

### Task 6: Phase 5 gate + compose app command

- [ ] **Step 1:** `docker compose run --rm test pytest -q` → Phases 1–4 (69) + Phase 5 (2+6+5+2+3 = 18) = **87 tests** pass.
- [ ] **Step 2:** Update `docker-compose.yml` `app` service command to `["erebus", "serve", "--config", "config/erebus.example.yaml"]` (replacing the placeholder). Commit `feat: wire compose app service to erebus serve`.
- [ ] **Step 3:** `grep -rn "shell=True\|os.system\|create_subprocess_shell\|sh -c" erebus/` → only matches, if any, are inside ClaudeCodeAdapter's `create_subprocess_exec` (which is the no-shell variant) — confirm no `_shell` variant present. Expected exit 1 (no forbidden matches).

---

## Self-Review

- **Spec coverage:** `AgentAdapter`/`RunContext`/`RunOutcome`/`LaunchSpec` (overview), Orchestrator state machine incl. on_deny resume/abort + TTL expiry (Decisions #4/#15/#16), `ClaudeCodeAdapter` (Decision #11; MCP config denies Bash, allows only the erebus tool, injects allowlist — Decisions #5/#6), FastAPI `POST /runs`+`GET /runs/{id}`+approve/deny (Decision #8), thin CLI (Decision #8). Stub-agent end-to-end test proves pause→approve→resume (Risk #1/#2 exercised at component level).
- **Placeholder scan:** none; subprocess/serve glue marked `pragma: no cover`.
- **Type consistency:** Orchestrator uses `RunStatus`/`RequestStatus`, `Store.list_pending_requests`/`set_request_status`/`set_run_status`/`set_run_session`/`get_run`/`create_run`, `TicketProvider.poll`, `LocalTicketProvider.get/approve/deny/list_pending`. `LaunchSpec` imported from `agents/base_spec.py` to avoid a cycle. `ClaudeCodeAdapter.parse_outcome` returns `RunOutcome` consistent with `start_run`/`_post_run` usage.

## Next

After Phase 5: Phase 6 (`SSHExecutor`), Phase 7 (`ZohoTicketProvider`), Phase 8 (`OpenCodeAdapter`). Also: background `POST /runs` execution (currently synchronous), and `POST /runs` auth before non-localhost exposure (Risk #6).
