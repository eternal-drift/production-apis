"""S2 - GraphQL acceptance tests."""

import asyncio
import json

import httpx
import pytest
import websockets

from production_apis.apis.graphql.app import create_graphql_app, start_graphql

CREATE = "mutation($input: CreateOrderInput!) { createOrder(input: $input) { id status totalCents customer { id } } }"


@pytest.fixture
async def gql(ctx, token):
    app = create_graphql_app(ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:

        async def call(query, variables=None, bearer=token):
            headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
            r = await c.post("/graphql", json={"query": query, "variables": variables}, headers=headers)
            return r.status_code, r.json()

        yield call


def order_input(sample_order, **over):
    return {"input": {"customerId": sample_order["customerId"], "items": sample_order["items"], **over}}


@pytest.mark.scenario("S2.3")
async def test_unauthenticated_is_rejected(gql):
    status, body = await gql("{ orders { nextCursor } }", bearer=None)
    assert status == 401 and body["errors"][0]["extensions"]["code"] == "UNAUTHENTICATED"


@pytest.mark.scenario("S2.1")
async def test_one_round_trip_for_detail_screen(gql, sample_order):
    _, created = await gql(CREATE, order_input(sample_order))
    oid = created["data"]["createOrder"]["id"]
    _, res = await gql(
        "query($id: ID!) { order(id: $id) { status items { sku quantity } customer { id orders { id } } } }",
        {"id": oid},
    )
    node = res["data"]["order"]
    assert (
        node["status"] == "PENDING" and len(node["items"]) == 2 and node["customer"]["orders"][0]["id"] == oid
    )
    _, missing = await gql('{ order(id: "00000000-0000-0000-0000-000000000000") { id } }')
    assert missing["data"]["order"] is None


@pytest.mark.scenario("S2.2")
async def test_dataloader_batches_customer_lookups(gql, ctx, sample_order):
    for _ in range(3):
        await gql(CREATE, order_input(sample_order, customerId="c-batch"))
    calls = 0
    original = ctx.orders.orders_by_customers

    async def counting(ids):
        nonlocal calls
        calls += 1
        return await original(ids)

    ctx.orders.orders_by_customers = counting
    _, res = await gql('{ orders(customerId: "c-batch") { edges { node { customer { orders { id } } } } } }')
    assert len(res["data"]["orders"]["edges"]) == 3 and calls == 1  # 3 orders, 1 batched lookup


@pytest.mark.scenario("S2.3")
async def test_domain_errors_become_typed_extensions(gql, sample_order):
    _, created = await gql(CREATE, order_input(sample_order))
    oid = created["data"]["createOrder"]["id"]
    _, bad = await gql(f'mutation {{ updateOrderStatus(id: "{oid}", status: SHIPPED) {{ id }} }}')
    assert bad["errors"][0]["extensions"]["code"] == "CONFLICT"
    _, invalid = await gql(CREATE, {"input": {"customerId": "x", "items": []}})
    assert invalid["errors"][0]["extensions"]["code"] == "VALIDATION"


async def test_scope_enforced(gql, ctx, sample_order, read_only_token):
    _, res = await gql(CREATE, order_input(sample_order), bearer=read_only_token)
    assert res["errors"][0]["extensions"]["code"] == "FORBIDDEN"


@pytest.mark.scenario("S2.6")
async def test_mutation_is_idempotent(gql, sample_order):
    a = await gql(CREATE, order_input(sample_order, idempotencyKey="gql-key-0001"))
    b = await gql(CREATE, order_input(sample_order, idempotencyKey="gql-key-0001"))
    assert a[1]["data"]["createOrder"]["id"] == b[1]["data"]["createOrder"]["id"]


@pytest.mark.scenario("S2.4")
async def test_depth_limit_and_introspection_policy(gql, ctx):
    deep = "{ orders { edges { node { " + "customer { orders { " * 5 + "id" + " } }" * 5 + " } } } }"
    _, res = await gql(deep)
    assert "depth" in res["errors"][0]["message"].lower()
    from .conftest import make_context

    prod_ctx = make_context(APP_ENV="production", jwt_secret="x" * 40)
    app = create_graphql_app(prod_ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        t = prod_ctx.auth.sign("u")
        r = await c.post(
            "/graphql",
            json={"query": "{ __schema { types { name } } }"},
            headers={"authorization": f"Bearer {t}"},
        )
        assert r.json().get("errors"), "introspection must be disabled in production"


@pytest.mark.scenario("S2.5")
async def test_live_status_subscription(ctx, token, sample_order):
    api = await start_graphql(ctx, 0)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{api.port}/graphql", subprotocols=["graphql-transport-ws"]
        ) as ws:
            await ws.send(
                json.dumps({"type": "connection_init", "payload": {"authorization": f"Bearer {token}"}})
            )
            assert json.loads(await ws.recv())["type"] == "connection_ack"
            await ws.send(
                json.dumps(
                    {
                        "id": "1",
                        "type": "subscribe",
                        "payload": {"query": "subscription { orderEvents { type order { id } } }"},
                    }
                )
            )
            await asyncio.sleep(0.2)
            order, _ = await ctx.orders.create(sample_order)
            msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert msg["type"] == "next"
            assert msg["payload"]["data"]["orderEvents"]["type"] == "ORDER_CREATED"
            assert msg["payload"]["data"]["orderEvents"]["order"]["id"] == str(order.id)
    finally:
        await api.close()


async def test_subscription_rejects_bad_token(ctx):
    api = await start_graphql(ctx, 0)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{api.port}/graphql", subprotocols=["graphql-transport-ws"]
        ) as ws:
            await ws.send(
                json.dumps({"type": "connection_init", "payload": {"authorization": "Bearer nope"}})
            )
            with pytest.raises(websockets.ConnectionClosed) as e:
                await asyncio.wait_for(ws.recv(), 3)
            assert e.value.rcvd.code == 4403
    finally:
        await api.close()
