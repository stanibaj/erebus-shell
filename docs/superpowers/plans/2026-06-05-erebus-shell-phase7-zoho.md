# Erebus Shell — Phase 7: Zoho Ticket Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implements the `TicketProvider` Protocol (Phase 3) against Zoho Desk. Phases 1–6 merged. `httpx` already a dependency. All runs in Docker.

**Goal:** A `ZohoTicketProvider` that creates a Zoho Desk ticket on escalation and polls a custom field for the human's approve/deny decision, with OAuth refresh-token → access-token caching — proving the `TicketProvider` abstraction against a real OAuth system.

**Architecture:** All HTTP goes through an injectable `httpx.AsyncClient`, so the entire flow (OAuth refresh, ticket create, custom-field polling, decision mapping) is unit-tested with `httpx.MockTransport` — no network, no Zoho account. Access tokens are cached until shortly before expiry and reused (Zoho caps active tokens, so a long-running daemon must not mint one per call). Approval is modeled as a Zoho custom field (Decision: "custom field is the lowest-friction approach"): the value mapping (`Approved`/`Denied`/else→pending) is configurable.

**Tech Stack:** `httpx` (+ `httpx.MockTransport` in tests), Phase 3 `TicketProvider`/`TicketRequest`/`TicketStatus`, `RequestStatus`, `pytest`.

---

### Task 1: ZohoConfig + ZohoTicketProvider

**Files:** Create `erebus/tickets/zoho.py`; Test `tests/tickets/test_zoho.py`.

- [ ] **Step 1: failing tests** — `tests/tickets/test_zoho.py`:

```python
import json
import httpx
import pytest

from erebus.tickets.zoho import ZohoTicketProvider, ZohoConfig
from erebus.tickets.base import TicketRequest
from erebus.state.models import RequestStatus


def _config():
    return ZohoConfig(
        base_url="https://desk.zoho.com/api/v1",
        accounts_url="https://accounts.zoho.com",
        client_id="cid", client_secret="secret", refresh_token="rt",
        org_id="org1", department_id="dep1", contact_id="con1",
        approval_field="cf_approval", approved_value="Approved", denied_value="Denied",
    )


def _provider(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return ZohoTicketProvider(_config(), client=client)


def _oauth_response():
    return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})


async def test_create_posts_ticket_and_returns_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/v2/token"):
            return _oauth_response()
        if request.method == "POST" and request.url.path.endswith("/tickets"):
            seen["auth"] = request.headers.get("Authorization")
            seen["org"] = request.headers.get("orgId")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "555000111"})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    p = _provider(handler)
    tid = await p.create(TicketRequest(run_id="r1", command="systemctl restart nginx",
                                       justification="service down"))
    assert tid == "555000111"
    assert seen["auth"] == "Zoho-oauthtoken tok-123"
    assert seen["org"] == "org1"
    assert "systemctl restart nginx" in json.dumps(seen["body"])
    assert seen["body"]["departmentId"] == "dep1"


async def test_poll_pending_when_field_unset():
    def handler(request):
        if request.url.path.endswith("/oauth/v2/token"):
            return _oauth_response()
        return httpx.Response(200, json={"id": "1", "customFields": {}})

    p = _provider(handler)
    status = await p.poll("1")
    assert status.decision is RequestStatus.PENDING
    assert status.ticket_id == "1"


async def test_poll_approved():
    def handler(request):
        if request.url.path.endswith("/oauth/v2/token"):
            return _oauth_response()
        return httpx.Response(200, json={"customFields": {"cf_approval": "Approved"}})

    p = _provider(handler)
    assert (await p.poll("1")).decision is RequestStatus.APPROVED


async def test_poll_denied():
    def handler(request):
        if request.url.path.endswith("/oauth/v2/token"):
            return _oauth_response()
        return httpx.Response(200, json={"customFields": {"cf_approval": "Denied"}})

    p = _provider(handler)
    assert (await p.poll("1")).decision is RequestStatus.DENIED


async def test_access_token_is_cached_across_calls():
    calls = {"oauth": 0}

    def handler(request):
        if request.url.path.endswith("/oauth/v2/token"):
            calls["oauth"] += 1
            return _oauth_response()
        if request.method == "POST":
            return httpx.Response(200, json={"id": "1"})
        return httpx.Response(200, json={"customFields": {"cf_approval": "Approved"}})

    p = _provider(handler)
    await p.create(TicketRequest(run_id="r", command="c", justification="j"))
    await p.poll("1")
    assert calls["oauth"] == 1            # token minted once, then reused


async def test_create_raises_on_http_error():
    def handler(request):
        if request.url.path.endswith("/oauth/v2/token"):
            return _oauth_response()
        return httpx.Response(400, json={"message": "bad"})

    p = _provider(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await p.create(TicketRequest(run_id="r", command="c", justification="j"))
```

- [ ] **Step 2: run, expect fail** (`ModuleNotFoundError: erebus.tickets.zoho`).

- [ ] **Step 3: implement** — `erebus/tickets/zoho.py`:

```python
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
```

- [ ] **Step 4: run, expect pass** (6 tests). **Step 5: commit** `feat(tickets): ZohoTicketProvider (OAuth + custom-field approval polling)`.

---

### Task 2: Phase 7 gate

- [ ] **Step 1:** `docker compose run --rm test pytest -q` → Phases 1–6 (97) + Phase 7 (6) = **103 tests** pass.

---

## Self-Review

- **Spec coverage:** `ZohoTicketProvider` satisfies the Phase 3 `TicketProvider` Protocol (`create`→`str`, `poll`→`TicketStatus`); OAuth refresh-token reuse (Decision #12 caveat: reuse one refresh token, cap on active tokens); approval as a custom field (overview Zoho notes). Fully tested via `httpx.MockTransport`.
- **Placeholder scan:** none; only the default-client construction is `pragma: no cover`.
- **Type consistency:** `create(TicketRequest)`/`poll(ticket_id)->TicketStatus` match the Protocol; `decision` uses `RequestStatus`. The orchestrator/gate consume any `TicketProvider`, so Zoho drops in without changes elsewhere.

## Next

Phase 8 (`OpenCodeAdapter`). Deployment for Zoho: store `refresh_token`/`client_secret` in the service env (never in agent context); document the custom field + a Blueprint/webhook upgrade path (Decision #12).
