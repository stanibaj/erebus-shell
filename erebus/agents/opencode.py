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
