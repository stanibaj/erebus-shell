from erebus.tickets.base import TicketRequest, TicketStatus, TicketProvider
from erebus.state.models import RequestStatus


def test_ticket_request_fields():
    req = TicketRequest(run_id="r1", command="systemctl restart nginx", justification="down")
    assert req.run_id == "r1"
    assert req.command == "systemctl restart nginx"
    assert req.justification == "down"


def test_ticket_status_fields_default_note_none():
    s = TicketStatus(ticket_id="T-1", decision=RequestStatus.PENDING)
    assert s.ticket_id == "T-1"
    assert s.decision is RequestStatus.PENDING
    assert s.note is None


def test_ticket_provider_is_protocol():
    assert hasattr(TicketProvider, "create")
    assert hasattr(TicketProvider, "poll")
