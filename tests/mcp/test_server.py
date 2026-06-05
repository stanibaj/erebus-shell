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
