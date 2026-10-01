"""S1 - REST API acceptance tests."""

import httpx
import pytest

from production_apis.apis.rest.app import create_rest_app

from .conftest import make_context

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(ctx, token):
    transport = httpx.ASGITransport(app=create_rest_app(ctx))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"authorization": f"Bearer {token}"}
    ) as c:
        yield c


@pytest.mark.scenario("S1.1")
async def test_requires_authentication_and_scope(client, ctx, sample_order, read_only_token):
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_rest_app(ctx)), base_url="http://t")
    r = await anon.get("/v1/orders")
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/problem+json")
    r = await client.post(
        "/v1/orders", json=sample_order, headers={"authorization": f"Bearer {read_only_token}"}
    )
    assert r.status_code == 403


@pytest.mark.scenario("S1.2")
async def test_idempotent_creation(client, sample_order):
    h = {"idempotency-key": "rest-key-0001"}
    first = await client.post("/v1/orders", json=sample_order, headers=h)
    assert first.status_code == 201 and first.headers["etag"] == '"1"'
    assert first.headers["location"] == f"/v1/orders/{first.json()['id']}"
    replay = await client.post("/v1/orders", json=sample_order, headers=h)
    assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    conflict = await client.post("/v1/orders", json={**sample_order, "customerId": "x"}, headers=h)
    assert conflict.status_code == 409
    assert len((await client.get("/v1/orders")).json()["items"]) == 1


@pytest.mark.scenario("S1.3")
async def test_validation_errors_are_actionable(client):
    r = await client.post("/v1/orders", json={"customerId": "x", "items": []})
    body = r.json()
    assert r.status_code == 400 and body["title"] == "VALIDATION" and body["requestId"]
    assert any("items" in e["field"] for e in body["errors"])
    r = await client.post(
        "/v1/orders", json={"customerId": "x", "items": []}, headers={"x-request-id": "trace-123"}
    )
    assert r.json()["requestId"] == "trace-123"


@pytest.mark.scenario("S1.4")
async def test_conditional_get(client, sample_order):
    order = (await client.post("/v1/orders", json=sample_order)).json()
    r = await client.get(f"/v1/orders/{order['id']}", headers={"if-none-match": '"1"'})
    assert r.status_code == 304 and r.content == b""
    assert (await client.get(f"/v1/orders/{order['id']}")).status_code == 200


@pytest.mark.scenario("S1.5")
async def test_lost_update_protection(client, sample_order):
    order = (await client.post("/v1/orders", json=sample_order)).json()
    url = f"/v1/orders/{order['id']}/status"
    assert (await client.patch(url, json={"status": "PAID"}, headers={"if-match": '"7"'})).status_code == 412
    ok = await client.patch(url, json={"status": "PAID"}, headers={"if-match": '"1"'})
    assert ok.status_code == 200 and ok.headers["etag"] == '"2"'
    assert (await client.patch(url, json={"status": "PENDING"})).status_code == 409


@pytest.mark.scenario("S1.6")
async def test_cursor_pagination(client, sample_order):
    for _ in range(3):
        await client.post("/v1/orders", json=sample_order)
    page = (await client.get("/v1/orders", params={"limit": 2})).json()
    assert len(page["items"]) == 2 and page["nextCursor"]
    rest = (await client.get("/v1/orders", params={"limit": 2, "cursor": page["nextCursor"]})).json()
    assert len(rest["items"]) == 1 and rest["nextCursor"] is None
    assert (await client.get("/v1/orders", params={"limit": 1000})).status_code == 400


@pytest.mark.scenario("S1.7")
async def test_rate_limiting_exempts_health():
    limited = make_context(rate_limit_max=3)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_rest_app(limited)), base_url="http://t"
    ) as c:
        codes = [(await c.get("/v1/orders")).status_code for _ in range(5)]
        assert 429 in codes
        assert (await c.get("/healthz")).status_code == 200
        r = await c.get("/v1/orders")
        assert r.headers["retry-after"]


async def test_ops_endpoints_and_headers(client):
    assert (await client.get("/readyz")).json() == {"status": "ready"}
    metrics = (await client.get("/metrics")).text
    assert "api_requests_total" in metrics
    r = await client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    big = await client.post(
        "/v1/orders", content=b"x" * 200_000, headers={"content-type": "application/json"}
    )
    assert big.status_code == 413
    spec = (await client.get("/openapi.json")).json()
    assert "/v1/orders" in spec["paths"]
