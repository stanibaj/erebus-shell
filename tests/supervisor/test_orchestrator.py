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
