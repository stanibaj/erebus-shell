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
