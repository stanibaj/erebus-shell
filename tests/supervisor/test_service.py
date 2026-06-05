import pytest
from fastapi.testclient import TestClient
from erebus.supervisor.service import create_app
from erebus.supervisor.orchestrator import Orchestrator
from erebus.agents.stub import StubAgentAdapter
from erebus.mcp.gate import CommandGate
from erebus.policy.engine import PolicyEngine
from erebus.policy.models import Policy, AllowRule
from erebus.executor.local import LocalExecutor
from erebus.tickets.local import LocalTicketProvider
from erebus.state.store import Store


@pytest.fixture()
def client(tmp_path):
    store = Store(str(tmp_path / "s.db")); store.init_schema()
    tickets = LocalTicketProvider(str(tmp_path / "t.db")); tickets.init_schema()
    engine = PolicyEngine(Policy(allow=[AllowRule(binary="echo")]))

    def gate_factory(run_id):
        return CommandGate(run_id=run_id, engine=engine, executor=LocalExecutor(),
                           tickets=tickets, store=store)

    stub = StubAgentAdapter(script=["whoami", "echo bye"], gate_factory=gate_factory)
    orch = Orchestrator(store=store, tickets=tickets, adapters={"stub": stub},
                        allowlist_text="echo")
    return TestClient(create_app(orchestrator=orch, tickets=tickets))


def test_create_run_pends_then_approve_completes(client):
    r = client.post("/runs", json={"task": "t", "agent": "stub"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "pending_approval"

    assert client.get(f"/runs/{run_id}").json()["status"] == "pending_approval"

    pend = client.get("/tickets/pending").json()
    assert len(pend) == 1
    ticket_id = pend[0]["id"]

    r2 = client.post(f"/tickets/{ticket_id}/approve", json={"note": "ok"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"
    assert client.get(f"/runs/{run_id}").json()["status"] == "completed"


def test_get_unknown_run_404(client):
    assert client.get("/runs/nope").status_code == 404
