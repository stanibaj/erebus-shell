"""TicketProvider interface: create an approval ticket and poll its decision."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from erebus.state.models import RequestStatus


@dataclass(frozen=True)
class TicketRequest:
    run_id: str
    command: str
    justification: str


@dataclass(frozen=True)
class TicketStatus:
    ticket_id: str
    decision: RequestStatus          # PENDING / APPROVED / DENIED
    note: str | None = None


class TicketProvider(Protocol):
    async def create(self, req: TicketRequest) -> str: ...   # returns ticket_id
    async def poll(self, ticket_id: str) -> TicketStatus: ...
