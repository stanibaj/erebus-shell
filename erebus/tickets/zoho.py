"""Zoho Desk ticket provider. Creates a ticket on escalation and polls a custom
field for the approve/deny decision. OAuth access tokens are cached and reused
(Zoho caps active tokens). All HTTP goes through an injectable httpx client so
the flow is fully testable with httpx.MockTransport.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from erebus.state.models import RequestStatus
from erebus.tickets.base import TicketRequest, TicketStatus


@dataclass(frozen=True)
class ZohoConfig:
    base_url: str
    accounts_url: str
    client_id: str
    client_secret: str
    refresh_token: str
    org_id: str
    department_id: str
    contact_id: str
    approval_field: str = "cf_approval"
    approved_value: str = "Approved"
    denied_value: str = "Denied"


class ZohoTicketProvider:
    def __init__(self, config: ZohoConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._cfg = config
        self._client = client or httpx.AsyncClient()  # pragma: no cover
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._token_expiry - 60:
            return self._token
        resp = await self._client.post(
            f"{self._cfg.accounts_url}/oauth/v2/token",
            params={
                "refresh_token": self._cfg.refresh_token,
                "client_id": self._cfg.client_id,
                "client_secret": self._cfg.client_secret,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.monotonic() + float(data.get("expires_in", 3600))
        return self._token

    async def _headers(self) -> dict[str, str]:
        token = await self._access_token()
        return {"Authorization": f"Zoho-oauthtoken {token}", "orgId": self._cfg.org_id}

    async def create(self, req: TicketRequest) -> str:
        body = {
            "subject": f"[Erebus] approval for: {req.command[:80]}",
            "description": (
                f"Command: {req.command}\n"
                f"Justification: {req.justification}\n"
                f"Run: {req.run_id}"
            ),
            "departmentId": self._cfg.department_id,
            "contactId": self._cfg.contact_id,
        }
        resp = await self._client.post(
            f"{self._cfg.base_url}/tickets", headers=await self._headers(), json=body
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    async def poll(self, ticket_id: str) -> TicketStatus:
        resp = await self._client.get(
            f"{self._cfg.base_url}/tickets/{ticket_id}",
            headers=await self._headers(),
            params={"include": "customFields"},
        )
        resp.raise_for_status()
        value = (resp.json().get("customFields") or {}).get(self._cfg.approval_field)
        if value == self._cfg.approved_value:
            decision = RequestStatus.APPROVED
        elif value == self._cfg.denied_value:
            decision = RequestStatus.DENIED
        else:
            decision = RequestStatus.PENDING
        return TicketStatus(ticket_id=ticket_id, decision=decision, note=value)
