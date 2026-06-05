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
