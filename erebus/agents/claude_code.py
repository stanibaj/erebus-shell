"""Claude Code adapter. Pure config/parse helpers are unit-tested; the subprocess
glue (`run`) is validated manually with the real `claude` CLI + ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from erebus.agents.base import LaunchSpec, RunContext, RunOutcome

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
    def _env(self, run_id: str) -> dict[str, str]:
        return {
            "EREBUS_RUN_ID": run_id,
            "EREBUS_DB_PATH": self._db_path,
            "EREBUS_TICKETS_DB": self._tickets_db,
            "EREBUS_POLICY_PATH": self._policy_path,
            "EREBUS_TTL_HOURS": str(self._ttl_hours),
        }

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

    def render_launch(self, ctx: RunContext) -> LaunchSpec:
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
