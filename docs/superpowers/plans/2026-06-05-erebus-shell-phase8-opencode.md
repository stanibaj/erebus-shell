# Erebus Shell — Phase 8: OpenCode Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implements a second `AgentAdapter` (Phase 5 contract) to prove the agent abstraction. Phases 1–7 merged. All runs in Docker.

**Goal:** An `OpenCodeAdapter` that configures an unmodified OpenCode so its only execution path is the erebus `run_command` MCP tool (native `bash` denied) and injects the allowlist — proving the `AgentAdapter` abstraction holds against a second, differently-shaped agent.

**Architecture:** Mirrors `ClaudeCodeAdapter`: pure `render_launch`/`parse_outcome` are unit-tested; the subprocess glue (`run`) is `pragma: no cover` and validated manually against the installed `opencode`. OpenCode is configured via a generated JSON config (`mcp` servers + `permission.bash = "deny"` + allowlist `instructions`) rather than Claude's CLI flags — the same intent expressed in OpenCode's idiom. Because exact OpenCode CLI flags/JSON output evolve, `parse_outcome` accepts both `session_id` and `sessionID`, and the flag layout is the documented-best-effort to be confirmed on first real run (like the Claude adapter's glue).

**Tech Stack:** Phase 5 `RunContext`/`RunOutcome`/`LaunchSpec`, `json`, `pytest`.

---

### Task 1: OpenCodeAdapter

**Files:** Create `erebus/agents/opencode.py`; Test `tests/agents/test_opencode.py`.

- [ ] **Step 1: failing tests** — `tests/agents/test_opencode.py`:

```python
import json
from erebus.agents.opencode import OpenCodeAdapter
from erebus.agents.base import RunContext


def _ctx(resume=False, session_id=None):
    return RunContext(run_id="run-9", task="inspect logs", allowlist_text="cat, ls",
                      resume=resume, session_id=session_id, message="continue")


def _adapter():
    return OpenCodeAdapter(policy_path="/cfg/p.yaml", db_path="/d/s.db", tickets_db="/d/t.db")


def test_render_launch_fresh_run():
    spec = _adapter().render_launch(_ctx())
    assert spec.cmd[0] == "opencode"
    assert "run" in spec.cmd
    assert "inspect logs" in spec.cmd
    assert "--session" not in spec.cmd
    # OpenCode config denies native bash and registers the erebus MCP server.
    assert spec.mcp_config["permission"]["bash"] == "deny"
    assert "erebus" in spec.mcp_config["mcp"]
    # allowlist is injected into the agent's instructions
    assert any("cat, ls" in line for line in spec.mcp_config["instructions"])
    assert spec.env["EREBUS_RUN_ID"] == "run-9"
    assert spec.env["EREBUS_POLICY_PATH"] == "/cfg/p.yaml"


def test_render_launch_resume_uses_session_and_message():
    spec = _adapter().render_launch(_ctx(resume=True, session_id="sess-77"))
    assert "--session" in spec.cmd
    assert "sess-77" in spec.cmd
    assert "continue" in spec.cmd          # message, not the original task


def test_mcp_server_command_is_erebus():
    spec = _adapter().render_launch(_ctx())
    server = spec.mcp_config["mcp"]["erebus"]
    assert server["type"] == "local"
    assert server["command"] == ["python", "-m", "erebus.mcp.server"]
    assert server["environment"]["EREBUS_RUN_ID"] == "run-9"


def test_parse_outcome_snake_case_session():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome(
        json.dumps({"session_id": "abc", "result": "ok"})
    )
    assert out.session_id == "abc"
    assert out.exit_code == 0


def test_parse_outcome_camel_case_session():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome(
        json.dumps({"sessionID": "xyz", "result": "ok"})
    )
    assert out.session_id == "xyz"


def test_parse_outcome_error_flag_sets_nonzero():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome(
        json.dumps({"sessionID": "x", "error": "boom"})
    )
    assert out.exit_code != 0


def test_parse_outcome_non_json():
    out = OpenCodeAdapter(policy_path="p", db_path="d", tickets_db="t").parse_outcome("oops")
    assert out.session_id is None
    assert out.exit_code != 0
```

- [ ] **Step 2: run, expect fail** (`ModuleNotFoundError: erebus.agents.opencode`).

- [ ] **Step 3: implement** — `erebus/agents/opencode.py`:

```python
"""OpenCode adapter. Pure config/parse helpers are unit-tested; the subprocess
glue (`run`) is validated manually against the installed `opencode` CLI.

OpenCode is configured (not flag-driven like Claude): a generated JSON config
registers the erebus MCP server, denies native bash, and injects the allowlist
as agent instructions.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from erebus.agents.base import LaunchSpec, RunContext, RunOutcome

_SYSTEM_PROMPT_TMPL = (
    "You operate under Erebus, a gating shell. Your ONLY way to run commands is the "
    "erebus `run_command` MCP tool — native bash is denied. These commands are allowed:\n"
    "{allowlist}\n"
    "Prefer an allowed command. If no allowed alternative exists, call run_command with "
    "the needed command and a clear reason; this creates a human approval ticket and "
    "pauses the run. Do not retry denied commands."
)


class OpenCodeAdapter:
    name = "opencode"

    def __init__(self, *, policy_path: str, db_path: str, tickets_db: str,
                 ttl_hours: float = 24.0) -> None:
        self._policy_path = policy_path
        self._db_path = db_path
        self._tickets_db = tickets_db
        self._ttl_hours = ttl_hours

    def _env(self, run_id: str) -> dict[str, str]:
        return {
            "EREBUS_RUN_ID": run_id,
            "EREBUS_DB_PATH": self._db_path,
            "EREBUS_TICKETS_DB": self._tickets_db,
            "EREBUS_POLICY_PATH": self._policy_path,
            "EREBUS_TTL_HOURS": str(self._ttl_hours),
        }

    def _config(self, ctx: RunContext) -> dict:
        prompt = _SYSTEM_PROMPT_TMPL.format(allowlist=ctx.allowlist_text)
        return {
            "mcp": {
                "erebus": {
                    "type": "local",
                    "command": ["python", "-m", "erebus.mcp.server"],
                    "environment": self._env(ctx.run_id),
                }
            },
            "permission": {"bash": "deny"},
            "instructions": [prompt],
        }

    def render_launch(self, ctx: RunContext) -> LaunchSpec:
        cmd = ["opencode", "run"]
        if ctx.resume and ctx.session_id:
            cmd += ["--session", ctx.session_id]
        cmd.append(ctx.message if ctx.resume else ctx.task)
        return LaunchSpec(cmd=cmd, env=self._env(ctx.run_id), mcp_config=self._config(ctx))

    def parse_outcome(self, raw_output: str) -> RunOutcome:
        try:
            data = json.loads(raw_output)
        except (json.JSONDecodeError, ValueError):
            return RunOutcome(session_id=None, exit_code=1, raw_output=raw_output)
        session_id = data.get("session_id") or data.get("sessionID")
        is_error = bool(data.get("error"))
        result = str(data.get("result", data.get("error", "")))
        return RunOutcome(session_id=session_id, exit_code=1 if is_error else 0,
                          raw_output=result)

    async def run(self, ctx: RunContext) -> RunOutcome:  # pragma: no cover
        spec = self.render_launch(ctx)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(spec.mcp_config, fh)
            cfg_path = fh.name
        cmd = spec.cmd + ["--config", cfg_path, "--format", "json"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, env={**os.environ, **spec.env},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return self.parse_outcome(out.decode(errors="replace"))
```

- [ ] **Step 4: run, expect pass** (7 tests). **Step 5: commit** `feat(agents): OpenCodeAdapter render_launch + parse_outcome`.

---

### Task 2: Phase 8 gate + README run notes

- [ ] **Step 1:** `docker compose run --rm test pytest -q` → Phases 1–7 (103) + Phase 8 (7) = **110 tests** pass.
- [ ] **Step 2:** Replace `README.md` with a short usage section (build/test via Docker, `docker compose up app`, the `erebus` CLI, env vars for ssh/zoho). Commit `docs: README usage for erebus-shell`.

---

## Self-Review

- **Spec coverage:** `OpenCodeAdapter` satisfies the Phase 5 `AgentAdapter` Protocol with the same `RunContext`/`RunOutcome`/`LaunchSpec` types as `ClaudeCodeAdapter`; denies native bash, registers the single erebus MCP tool, injects the allowlist (Decisions #5/#6/#11 — second agent). Proves the adapter abstraction. Subprocess glue is `pragma: no cover`, validated manually.
- **Placeholder scan:** none; only `run` is `pragma: no cover`.
- **Type consistency:** `render_launch -> LaunchSpec`, `parse_outcome -> RunOutcome` match `ClaudeCodeAdapter` and the orchestrator's expectations; `parse_outcome` tolerates both `session_id`/`sessionID`.

## Done

All 8 phases complete. Remaining real-world validation (not code): run the real Claude Code / OpenCode subprocess paths with credentials to confirm stop-on-pending + exact-command resume (Risks #1/#2), and wire `OpenCodeAdapter`/`ZohoTicketProvider`/`SSHExecutor` selection into `bootstrap.py` for production configs.
