# Erebus Shell — Implementation Handoff & Architecture Overview

> **For agentic workers:** This is the master design + roadmap. It is the *spec*. Each phase below has (or will have) its own bite-sized TDD plan file in this directory. Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement each phase plan task-by-task. Implement phases in order — later phases depend on earlier interfaces.

**Goal:** A general-purpose gating "shell" that wraps an unmodified AI coding agent (Claude Code first, OpenCode later), forces every command through a single allowlist-checked chokepoint, and — when the agent decides it genuinely needs a not-allowed command — creates an approval ticket, pauses the run, and resumes the agent on the same session once a human approves.

**Architecture (one paragraph):** A long-lived **HTTP service** is the public surface (`POST /runs` to start a run; `GET /runs/{id}` for status). External trigger projects (a Netdata poller, a webhook bridge, the bundled CLI) are just clients of that API. To run a task, the **supervisor** launches the agent configured so its *only* execution tool is a generic `run_command` MCP tool exposed by our **gating MCP server**; the agent's native shell/Bash is denied. The MCP server is the sole executor: it parses the command into `argv`, executes it **without a shell** (no `&&`/`|`/`$()` interpretation) if it matches the YAML allowlist, and otherwise creates a ticket via a pluggable `TicketProvider`, records a pending request, and returns a "pending" result. The supervisor sees the pending request, ends the run cleanly, polls the ticket, and on approval **resumes the same agent session** (`claude --resume <session_id>`); the agent re-issues the command, the MCP server now sees it approved, executes it, and returns the output. Durable state lives in SQLite so an approval survives a service restart.

**Tech Stack:** Python 3.11+, FastAPI (HTTP service), official MCP Python SDK (`mcp`), `asyncssh` (remote executor, later), stdlib `sqlite3` (state), `pydantic` v2 (config/policy validation), `PyYAML` (policy file), `pytest` + `pytest-asyncio` (tests). Package name: `erebus`.

---

## Resolved Design Decisions (the grilling results — do not relitigate)

These were resolved with the user via the grill-me skill. Treat them as fixed constraints.

1. **Wrap, don't replace.** We never replace `/bin/sh`. We wrap an *unmodified* agent and configure it.
2. **General framework, not a one-off app.** Agent-agnostic, integration-agnostic. The homelab / Netdata / Zoho scenario is a *reference deployment*, not the core.
3. **Single shared gating service; thin per-agent adapters.** One service holds the allowlist, ticket logic, and state. An "adapter" is mostly config generation + lifecycle commands, **not** plugin code.
4. **Async deny-and-resume is the uniform contract.** A blocked command returns a "pending" result and the run ends cleanly; the supervisor resumes the session later on approval. (Claude's `defer` may be used internally as an optimization but is **not** in the core contract.)
5. **Execution chokepoint via one generic MCP tool.** The agent gets a single `run_command` tool from our MCP server; native Bash/shell tools are **denied**. The MCP server is the **sole executor and credential broker** — the agent never holds credentials and never runs commands itself. (This supersedes any earlier "native Bash via hooks" idea; the credential-broker requirement forced the chokepoint.)
6. **Allowlist awareness via context injection; ticket on first attempt.** The current allowlist + escalation policy is injected into the agent's system prompt so it prefers allowed commands and only invokes `run_command` with a not-allowed string as a genuine last resort. There is **no special approval tool** — the act of calling `run_command` with a not-allowed command *is* the confirmation, and a ticket is created on the **first** such attempt. The justification for the ticket is harvested from the agent's reasoning text (plus an optional free-text reason the agent is instructed to include).
7. **No-shell `argv` execution + per-binary arg-pattern allowlist.** Commands are executed with `execve`-style direct exec, never `sh -c`. Shell metacharacters cannot be interpreted; this eliminates the `a && b` / `$()` / pipe bypass class. A genuine pipeline must be an explicit, flagged "shell-mode" allowlist entry (rare escape hatch, later).
8. **Public interface is an HTTP service; CLI is a thin client.** Triggers are external projects (any language) that call `POST /runs`. The bundled CLI (`erebus run`) just calls the same API.
9. **Triggers are external plug-ins.** A Netdata/alerting poller is a *separate project* that calls `POST /runs`. We define and document the ingress contract; we ship only a manual CLI trigger as reference.
10. **Python.** See tech stack.
11. **Claude Code is the first agent adapter** (cleanest lifecycle: `--output-format json` returns the session id, `--resume <id>` is first-class). OpenCode is second.
12. **Local reference `TicketProvider` first, Zoho second** (so the full loop is testable with zero external deps). GitHub Issues is an easy third.
13. **SQLite** for durable state (runs, pending requests, audit log). Postgres only if HA is needed later.
14. **YAML** for the human-editable policy (allowlist rules, executor targets, agent/ticket backend selection), validated by a pydantic schema on load.
15. **On denial: resume the agent with a denial message** ("denied by a human; do not retry; find an allowed alternative or stop").
16. **Pending requests expire** on a configurable TTL (default 24h); expiry is treated as a denial. Stops zombie runs and unbounded polling.

---

## File Structure

```
erebus-shell/
├── pyproject.toml
├── erebus/
│   ├── __init__.py
│   ├── config.py                 # load + validate YAML (pydantic): ErebusConfig
│   ├── policy/
│   │   ├── __init__.py
│   │   ├── parsing.py            # parse_command(str) -> ParsedCommand; shell-operator detection
│   │   ├── models.py             # AllowRule, ArgConstraint, Policy (pydantic)
│   │   └── engine.py             # PolicyEngine.evaluate(argv) -> Decision
│   ├── state/
│   │   ├── __init__.py
│   │   ├── models.py             # Run, PendingRequest, AuditEntry, enums
│   │   └── store.py              # Store: SQLite-backed CRUD (runs/pending/audit)
│   ├── executor/
│   │   ├── __init__.py
│   │   ├── base.py               # Executor protocol; ExecResult
│   │   ├── local.py              # LocalExecutor (no-shell subprocess)
│   │   └── ssh.py                # SSHExecutor (asyncssh, no-shell remote)        [Phase 6]
│   ├── tickets/
│   │   ├── __init__.py
│   │   ├── base.py               # TicketProvider protocol; TicketStatus, TicketRequest
│   │   ├── local.py              # LocalTicketProvider (SQLite-backed; CLI/HTTP approve)
│   │   └── zoho.py               # ZohoTicketProvider                              [Phase 7]
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── server.py             # MCP server exposing run_command; wires policy+executor+tickets+store
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py               # AgentAdapter protocol
│   │   └── claude_code.py        # ClaudeCodeAdapter
│   ├── supervisor/
│   │   ├── __init__.py
│   │   ├── service.py            # FastAPI app: POST /runs, GET /runs/{id}, approve/deny (local)
│   │   └── orchestrator.py       # run lifecycle state machine: launch->pending->poll->resume/deny/expire
│   └── cli.py                    # erebus run | serve | approve | deny  (thin API client)
├── tests/
│   └── (mirrors erebus/ package layout)
├── docs/superpowers/plans/
│   ├── 2026-06-05-erebus-shell-overview.md          # this file
│   ├── 2026-06-05-erebus-shell-phase1-policy-state.md
│   └── (later phase plans)
└── config/
    └── erebus.example.yaml       # reference policy + service config
```

**Responsibility boundaries:**
- `policy/` — pure functions, no I/O. Given `argv`, decide allow/block. Trivially testable.
- `state/` — the only module that touches SQLite. Everything else goes through `Store`.
- `executor/` — the only module that runs external processes / SSH. Holds credentials.
- `tickets/` — the only module that talks to a ticketing system.
- `mcp/` — wires policy + executor + tickets + store into the `run_command` tool. No business logic of its own beyond orchestration of those four.
- `agents/` — per-agent config generation + launch/resume/parse. Thin.
- `supervisor/` — owns run lifecycle and the HTTP surface. Never executes commands itself.

---

## Core Interfaces (locked — keep signatures consistent across phases)

```python
# policy/parsing.py
@dataclass(frozen=True)
class ParsedCommand:
    argv: list[str]
    contains_operators: bool
    operators_found: list[str]

class CommandParseError(Exception): ...

def parse_command(command: str) -> ParsedCommand: ...

# policy/engine.py
class DecisionType(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"

@dataclass(frozen=True)
class Decision:
    type: DecisionType
    reason: str
    matched_rule: str | None = None

class PolicyEngine:
    def __init__(self, policy: "Policy") -> None: ...
    def evaluate(self, argv: list[str]) -> Decision: ...

# state/models.py
class RunStatus(str, Enum):
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"

class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"

# state/store.py  (key methods — full set in Phase 1 plan)
class Store:
    def __init__(self, db_path: str) -> None: ...
    def init_schema(self) -> None: ...
    def create_run(self, agent: str, task: str) -> str: ...            # returns run_id
    def get_run(self, run_id: str) -> Run | None: ...
    def set_run_status(self, run_id: str, status: RunStatus) -> None: ...
    def set_run_session(self, run_id: str, session_id: str) -> None: ...
    def create_pending_request(self, run_id: str, command: str,
                               justification: str, ticket_id: str,
                               expires_at: str) -> str: ...            # returns request_id
    def find_request(self, run_id: str, command: str) -> PendingRequest | None: ...
    def set_request_status(self, request_id: str, status: RequestStatus) -> None: ...
    def list_pending_requests(self) -> list[PendingRequest]: ...
    def add_audit(self, run_id: str, event: str, command: str | None,
                  decision: str | None, detail: str | None) -> None: ...

# executor/base.py
@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

class Executor(Protocol):
    async def execute(self, argv: list[str], *, cwd: str | None = None,
                      timeout: float | None = None) -> ExecResult: ...

# tickets/base.py
@dataclass(frozen=True)
class TicketRequest:
    run_id: str
    command: str
    justification: str

@dataclass(frozen=True)
class TicketStatus:
    ticket_id: str
    decision: RequestStatus   # PENDING / APPROVED / DENIED
    note: str | None = None

class TicketProvider(Protocol):
    async def create(self, req: TicketRequest) -> str: ...       # returns ticket_id
    async def poll(self, ticket_id: str) -> TicketStatus: ...

# agents/base.py
@dataclass(frozen=True)
class LaunchSpec:
    cmd: list[str]            # argv to launch the agent process
    env: dict[str, str]       # extra env (EREBUS_RUN_ID, EREBUS_DB_PATH, MCP config path, etc.)

@dataclass(frozen=True)
class RunOutcome:
    session_id: str | None
    exit_code: int
    raw_output: str

class AgentAdapter(Protocol):
    name: str
    def render_launch(self, *, run_id: str, task: str,
                      allowlist_text: str, mcp_config_path: str) -> LaunchSpec: ...
    def render_resume(self, *, run_id: str, session_id: str,
                      message: str, mcp_config_path: str) -> LaunchSpec: ...
    def parse_outcome(self, raw_output: str) -> RunOutcome: ...
```

### The `run_command` MCP tool contract (the heart of the system)

Exposed by `erebus/mcp/server.py`. The MCP server process is spawned by the agent (stdio) using a config the supervisor renders; it receives `EREBUS_RUN_ID` and `EREBUS_DB_PATH` via env and connects to the shared SQLite `Store`.

Tool: `run_command(command: str, reason: str = "") -> str`

Algorithm:
1. `parsed = parse_command(command)`. If `parsed.contains_operators`: **BLOCK** with a clear message ("Shell operators (`&&`, `|`, `;`, `$()`, redirects) are not supported. Issue a single command, or if a pipeline is essential, explain why and request approval."). Audit it. Return the message (do **not** create a ticket for a malformed/operator command — it's a usage error, not an escalation).
2. `decision = engine.evaluate(parsed.argv)`.
3. If `ALLOW`: `result = await executor.execute(parsed.argv)`. Audit `executed`. Return formatted stdout/stderr/exit.
4. If `BLOCK`:
   a. `existing = store.find_request(run_id, command)`.
      - If `existing` and `APPROVED`: execute (as in step 3), set request resolved, audit `executed_after_approval`, return output.
      - If `existing` and `DENIED`/`EXPIRED`: return the denial message ("This command was denied by a human. Do not retry it. Use an allowed alternative or stop.").
      - If `existing` and `PENDING`: return the pending message (idempotent).
   b. Else (first attempt → **ticket on first attempt**): build justification from `reason` (+ note that fuller rationale is in the run transcript); `ticket_id = await ticket_provider.create(TicketRequest(run_id, command, justification))`; `store.create_pending_request(...)` with `expires_at = now + ttl`; `store.set_run_status(run_id, PENDING_APPROVAL)`; audit `escalated`. Return the pending message: "This command is not on the allowlist. A request for human approval has been created (ticket {id}). This run will pause; it resumes automatically if approved. Stop now."

> **Run-ends-cleanly requirement (Decision #4):** After a pending result, the agent should stop. We rely on (a) the instruction in the pending message, and (b) the supervisor capping the agent with `--max-turns` as a backstop, and (c) the supervisor observing `run.status == PENDING_APPROVAL` in the store and terminating the agent process if it is still running once a pending request exists. Phase 5 implements (c).

### Supervisor run lifecycle (state machine — Phase 5)

```
POST /runs(task, agent)
  -> store.create_run -> RUNNING
  -> orchestrator.launch(): render MCP config + launch spec; spawn agent; capture session_id from outcome
  -> on agent exit:
       if a PENDING request exists for this run:
           run.status = PENDING_APPROVAL
           enqueue for polling
       elif outcome ok:
           run.status = COMPLETED
       else:
           run.status = FAILED
Polling loop (per pending request, until resolved or expired):
  status = await ticket_provider.poll(ticket_id)
  if APPROVED:  store.set_request_status(APPROVED); resume(run, "The request was approved; the command will now succeed. Continue.")
  if DENIED:    store.set_request_status(DENIED);   resume(run, "The request was denied. Do not retry; use an allowed alternative or stop.")
  if now > expires_at: store.set_request_status(EXPIRED); run.status = EXPIRED  (treat as denial; no resume, or resume-with-denial per config)
resume(run, message):
  render_resume(session_id, message) -> spawn agent --resume
  -> on exit, re-evaluate (may complete, fail, or create a NEW pending request -> back to PENDING_APPROVAL)
```

---

## Phased Roadmap

Each phase is a separate plan file and produces working, tested software on its own.

| Phase | Plan file | Delivers | Depends on |
|------|-----------|----------|------------|
| **1** | `...-phase1-policy-state.md` (written) | `policy/` (parsing, models, engine) + `state/` (store) + project scaffold. Pure logic, fully unit-tested, no network/process. | — |
| **2** | `...-phase2-executor.md` | `executor/base.py` + `LocalExecutor` (no-shell subprocess exec, timeout, capture). | 1 |
| **3** | `...-phase3-tickets.md` | `tickets/base.py` + `LocalTicketProvider` (SQLite-backed; approve/deny via store; used by CLI/HTTP later). | 1 |
| **4** | `...-phase4-mcp-server.md` | `mcp/server.py` — the `run_command` tool wiring policy+executor+tickets+store. Integration-tested via an in-process MCP client. | 1,2,3 |
| **5** | `...-phase5-supervisor.md` | `agents/claude_code.py`, `supervisor/orchestrator.py`, `supervisor/service.py` (FastAPI `POST /runs`,`GET /runs/{id}`,`approve/deny`), `cli.py`. End-to-end loop with the **local** ticket provider and a **stub agent** in tests; real Claude Code behind a flag. | 1–4 |
| **6** | `...-phase6-ssh-executor.md` | `SSHExecutor` (asyncssh, no-shell remote exec; credentials in service env). Reference homelab deployment doc. | 2 |
| **7** | `...-phase7-zoho.md` | `ZohoTicketProvider` (OAuth refresh-token reuse, custom-field approval polling). Proves the `TicketProvider` abstraction against a real system. | 3,5 |
| **8** | `...-phase8-opencode.md` | `OpenCodeAdapter` — proves the `AgentAdapter` abstraction against the messier resume model. | 5 |

**Build order rationale:** Phases 1–5 give a complete, demoable system (local ticket provider + local executor + Claude Code) with the riskiest mechanics — the async pause/resume and the no-shell chokepoint — validated first and against zero external dependencies. Phases 6–8 swap in real-world adapters, each a clean test of one abstraction.

---

## Security Hardening Checklist (carry into every phase; from the research report's caveats)

- [ ] **No-shell exec is mandatory.** `LocalExecutor`/`SSHExecutor` MUST use list-argv exec, never `shell=True` / `sh -c`. Add a test asserting a command with `&&` does not chain.
- [ ] **Deny-wins.** `deny_binaries` is checked before the allowlist.
- [ ] **Secrets never enter the agent context.** Credentials (SSH keys, Zoho/Netdata tokens) live only in the service/executor process env. The agent only ever sends/receives command strings + output.
- [ ] **Least-privilege execution target.** Reference deployment runs the executor's SSH against a least-privilege remote user; document this.
- [ ] **Deny secret-file reads** even via allowed binaries: the policy should not allowlist `cat`/`less` on arbitrary paths; constrain arg patterns. Document `Read(.env*)`/`~/.ssh/**`-equivalent guidance.
- [ ] **Write-then-execute defense:** do not allowlist arbitrary interpreters (`bash`, `python`, `sh`) on attacker-controlled script paths; rely on least-privilege + arg constraints, not string matching.
- [ ] **Cost/runaway guards:** supervisor sets `--max-turns` and (where supported) a budget cap per agent run; dedupe identical pending requests per run (the `find_request` idempotency in the tool already helps).
- [ ] **Pin agent versions** in the reference deployment; the report notes Claude Code hook regressions across versions — though our MCP-tool approach avoids the hook path, still pin.
- [ ] **Audit everything.** Every proposed command, decision, escalation, and execution is written to `audit_log`.

---

## Open Questions / Risks (resolve during implementation, not blocking)

1. **Forcing the run to end on pending (Decision #4 backstop).** Verify empirically that Claude Code, after a `run_command` tool returns the "pending — stop now" message, actually ends its turn. If it loops, lean harder on `--max-turns=1`-style caps and supervisor-side process termination when `status==PENDING_APPROVAL`. *Validate in Phase 5.*
2. **Session-id resume fidelity.** Confirm `claude -p --resume <session_id>` re-establishes the MCP server connection and that the re-issued `run_command` is recognized as the same pending request (string-exact match of `command`). If the agent rephrases the command on resume, `find_request` won't match — mitigate by having the approval message tell the agent to issue the *exact same command*, and/or match on a normalized argv rather than raw string. *Validate in Phase 5.*
3. **Per-run MCP server identity.** Decided: supervisor spawns the MCP server via the agent's stdio MCP config with `EREBUS_RUN_ID` in env. Confirm Claude Code passes through env to stdio MCP servers; if not, encode `run_id` into the MCP server command args. *Validate in Phase 4/5.*
4. **Operator detection false-positives.** The conservative metacharacter scan may block legitimate args containing `>`/`|`/`$`. Accept for v1 (escalation path exists); revisit with a proper quote-aware scanner if it bites. *Phase 1.*
5. **Concurrent runs.** v1 assumes a modest number of concurrent runs sharing one SQLite file (WAL mode). If concurrency grows, revisit (Postgres, Decision #13). *Not blocking.*
6. **Trigger ingress auth.** `POST /runs` needs authentication before any non-localhost exposure (bearer token / mTLS). v1 binds localhost only. *Phase 5 + deployment doc.*

---

## How to start

1. Create an isolated worktree (`superpowers:using-git-worktrees`).
2. Open `2026-06-05-erebus-shell-phase1-policy-state.md` and execute it with `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`.
3. Phases 2–8 plans are written on demand once the prior phase's interfaces are real (keeps later code consistent with what actually got built).
