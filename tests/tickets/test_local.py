import pytest
from erebus.tickets.local import LocalTicketProvider, Ticket
from erebus.tickets.base import TicketRequest
from erebus.state.models import RequestStatus


@pytest.fixture()
def provider(tmp_path):
    p = LocalTicketProvider(str(tmp_path / "tickets.db"))
    p.init_schema()
    return p


def _req(cmd="systemctl restart nginx"):
    return TicketRequest(run_id="r1", command=cmd, justification="service is down")


async def test_create_starts_pending(provider):
    tid = await provider.create(_req())
    assert isinstance(tid, str) and tid
    status = await provider.poll(tid)
    assert status.ticket_id == tid
    assert status.decision is RequestStatus.PENDING
    assert status.note is None


async def test_approve_then_poll(provider):
    tid = await provider.create(_req())
    provider.approve(tid, note="looks fine")
    status = await provider.poll(tid)
    assert status.decision is RequestStatus.APPROVED
    assert status.note == "looks fine"


async def test_deny_then_poll(provider):
    tid = await provider.create(_req())
    provider.deny(tid, note="too risky")
    status = await provider.poll(tid)
    assert status.decision is RequestStatus.DENIED
    assert status.note == "too risky"


async def test_poll_unknown_ticket_raises(provider):
    with pytest.raises(KeyError):
        await provider.poll("does-not-exist")


async def test_list_pending_returns_unresolved_with_context(provider):
    tid1 = await provider.create(_req("cmd-a"))
    tid2 = await provider.create(_req("cmd-b"))
    provider.approve(tid1)
    pending = provider.list_pending()
    assert len(pending) == 1
    t = pending[0]
    assert isinstance(t, Ticket)
    assert t.id == tid2
    assert t.command == "cmd-b"
    assert t.justification == "service is down"
    assert t.run_id == "r1"


async def test_get_returns_full_ticket(provider):
    tid = await provider.create(_req("cmd-x"))
    t = provider.get(tid)
    assert t.id == tid
    assert t.command == "cmd-x"
    assert t.decision is RequestStatus.PENDING


async def test_persists_across_reopen(tmp_path):
    db = str(tmp_path / "tickets.db")
    p1 = LocalTicketProvider(db)
    p1.init_schema()
    tid = await p1.create(_req("persist"))
    p2 = LocalTicketProvider(db)            # simulate restart
    status = await p2.poll(tid)
    assert status.decision is RequestStatus.PENDING
