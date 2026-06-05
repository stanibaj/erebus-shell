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
