import pytest
from erebus.state.store import Store
from erebus.state.models import RunStatus, RequestStatus


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "erebus.db"))
    s.init_schema()
    return s


def test_create_and_get_run(store):
    run_id = store.create_run(agent="claude_code", task="check disk")
    run = store.get_run(run_id)
    assert run is not None
    assert run.id == run_id
    assert run.agent == "claude_code"
    assert run.task == "check disk"
    assert run.status is RunStatus.RUNNING
    assert run.session_id is None


def test_get_missing_run_returns_none(store):
    assert store.get_run("nope") is None


def test_set_run_status(store):
    run_id = store.create_run(agent="claude_code", task="t")
    store.set_run_status(run_id, RunStatus.COMPLETED)
    assert store.get_run(run_id).status is RunStatus.COMPLETED


def test_set_run_session(store):
    run_id = store.create_run(agent="claude_code", task="t")
    store.set_run_session(run_id, "sess-abc")
    assert store.get_run(run_id).session_id == "sess-abc"


def test_create_and_find_pending_request(store):
    run_id = store.create_run(agent="claude_code", task="t")
    req_id = store.create_pending_request(
        run_id=run_id, command="systemctl restart nginx",
        justification="service is down", ticket_id="T-1",
        expires_at="2026-06-06T00:00:00",
    )
    found = store.find_request(run_id, "systemctl restart nginx")
    assert found is not None
    assert found.id == req_id
    assert found.status is RequestStatus.PENDING
    assert found.ticket_id == "T-1"


def test_find_request_no_match_returns_none(store):
    run_id = store.create_run(agent="claude_code", task="t")
    assert store.find_request(run_id, "anything") is None


def test_set_request_status(store):
    run_id = store.create_run(agent="claude_code", task="t")
    req_id = store.create_pending_request(
        run_id=run_id, command="c", justification="j",
        ticket_id="T-2", expires_at="2026-06-06T00:00:00",
    )
    store.set_request_status(req_id, RequestStatus.APPROVED)
    assert store.find_request(run_id, "c").status is RequestStatus.APPROVED


def test_list_pending_requests_only_returns_pending(store):
    run_id = store.create_run(agent="claude_code", task="t")
    p = store.create_pending_request(run_id=run_id, command="a", justification="j",
                                     ticket_id="T-3", expires_at="2026-06-06T00:00:00")
    store.create_pending_request(run_id=run_id, command="b", justification="j",
                                 ticket_id="T-4", expires_at="2026-06-06T00:00:00")
    store.set_request_status(p, RequestStatus.APPROVED)
    pending = store.list_pending_requests()
    assert {r.command for r in pending} == {"b"}


def test_audit_log_append(store):
    run_id = store.create_run(agent="claude_code", task="t")
    store.add_audit(run_id, event="executed", command="ls", decision="allow", detail=None)
    store.add_audit(run_id, event="escalated", command="rm x", decision="block", detail="ticket T-5")
    entries = store.list_audit(run_id)
    assert len(entries) == 2
    assert entries[0].event == "executed"
    assert entries[1].detail == "ticket T-5"


def test_state_survives_reopen(tmp_path):
    db = str(tmp_path / "erebus.db")
    s1 = Store(db)
    s1.init_schema()
    run_id = s1.create_run(agent="claude_code", task="persist me")
    # New Store instance on the same file (simulates a service restart).
    s2 = Store(db)
    assert s2.get_run(run_id).task == "persist me"
