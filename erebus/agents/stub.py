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
