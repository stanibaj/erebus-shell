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
    tickets.approve(req.ticket_id)                                  # human approves the ticket
    store.set_request_status(req.id, RequestStatus.APPROVED)        # supervisor reconciled
    await stub.run(RunContext(run_id=run_id, task="t", allowlist_text="",
                              resume=True, session_id="s", message="approved"))
    # whoami executed after approval, then echo bye executed; no pending left.
    events = [e.event for e in store.list_audit(run_id)]
    assert "executed_after_approval" in events
    assert tickets.list_pending() == []
