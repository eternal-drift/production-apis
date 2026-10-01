"""S4 - WebSocket acceptance tests."""

import asyncio
import json

import pytest
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from production_apis.apis.websocket.server import start_websocket

from .conftest import make_context


@pytest.fixture
async def ws_api(ctx):
    api = await start_websocket(ctx, 0)
    yield ctx, f"ws://127.0.0.1:{api.port}/ws"
    await api.close()


async def recv(ws, timeout=3):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def connect(url, token):
    ws = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    assert (await recv(ws))["type"] == "hello"
    return ws


@pytest.mark.scenario("S4.1")
async def test_handshake_rejects_unauthenticated_before_upgrade(ws_api, token, read_only_token):
    _, url = ws_api
    with pytest.raises(InvalidStatus) as e:
        await websockets.connect(url)
    assert e.value.response.status_code == 401
    with pytest.raises(InvalidStatus) as e:
        await websockets.connect(url, additional_headers={"Authorization": "Bearer garbage"})
    assert e.value.response.status_code == 401
    # Browser flow: token delivered via Sec-WebSocket-Protocol; the token is never echoed back.
    async with websockets.connect(url, subprotocols=["bearer", token]) as ws:
        assert ws.subprotocol == "bearer"
        assert (await recv(ws))["type"] == "hello"


@pytest.mark.scenario("S4.2")
async def test_live_feed(ws_api, token, sample_order):
    ctx, url = ws_api
    async with await connect(url, token) as ws:
        await ws.send(json.dumps({"type": "subscribe"}))
        assert (await recv(ws))["type"] == "subscribed"
        order, _ = await ctx.orders.create(sample_order)
        msg = await recv(ws)
        assert (
            msg["type"] == "event" and msg["event"] == "order.created" and msg["order"]["id"] == str(order.id)
        )


@pytest.mark.scenario("S4.3")
async def test_change_filter_without_reconnecting(ws_api, token, sample_order):
    ctx, url = ws_api
    async with await connect(url, token) as ws:
        await ws.send(json.dumps({"type": "subscribe", "customerId": "C1"}))
        await recv(ws)
        await ctx.orders.create({**sample_order, "customerId": "C2"})
        mine, _ = await ctx.orders.create({**sample_order, "customerId": "C1"})
        assert (await recv(ws))["order"]["id"] == str(mine.id)  # C2's order was filtered out
        await ws.send(json.dumps({"type": "subscribe", "customerId": "C2"}))
        await recv(ws)
        other, _ = await ctx.orders.create({**sample_order, "customerId": "C2"})
        assert (await recv(ws))["order"]["id"] == str(other.id)
        await ws.send(json.dumps({"type": "ping"}))
        assert (await recv(ws))["type"] == "pong"


@pytest.mark.scenario("S4.4")
async def test_dead_peer_is_terminated_by_heartbeat():
    ctx = make_context(ws_heartbeat_seconds=0.2)
    api = await start_websocket(ctx, 0)
    try:
        token = ctx.auth.sign("u")
        reader, writer = await asyncio.open_connection("127.0.0.1", api.port)
        # A raw TCP client that completes the handshake but never answers pings (a "frozen laptop").
        writer.write(
            (
                f"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {token}\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        await asyncio.sleep(0.2)
        assert ctx.metrics.connections.labels("websocket")._value.get() == 1
        await asyncio.sleep(1.0)
        assert (
            ctx.metrics.connections.labels("websocket")._value.get() == 0
        )  # server cleaned up the dead connection
        writer.close()
    finally:
        await api.close()


@pytest.mark.scenario("S4.5")
async def test_protection_limits(token):
    ctx = make_context(ws_max_message_bytes=256, ws_max_connections=1)
    api = await start_websocket(ctx, 0)
    url = f"ws://127.0.0.1:{api.port}/ws"
    try:
        t = ctx.auth.sign("u")
        ws = await connect(url, t)
        with pytest.raises(InvalidStatus) as e:  # connection cap
            await websockets.connect(url, additional_headers={"Authorization": f"Bearer {t}"})
        assert e.value.response.status_code == 503
        await ws.send("x" * 1000)  # oversized frame
        with pytest.raises(ConnectionClosed) as c:
            await asyncio.wait_for(ws.recv(), 2)
        assert c.value.rcvd.code == 1009

        ws2 = await connect(url, t)
        for _ in range(30):  # flood: exceeds the 20-message burst
            await ws2.send(json.dumps({"type": "ping"}))
        with pytest.raises(ConnectionClosed) as c:
            for _ in range(40):
                await asyncio.wait_for(ws2.recv(), 2)
        assert c.value.rcvd.code == 1008
    finally:
        await api.close()


@pytest.mark.scenario("S4.5")
async def test_origin_allow_list():
    ctx = make_context(ws_allowed_origins=["https://wall.northwind.example"])
    api = await start_websocket(ctx, 0)
    url = f"ws://127.0.0.1:{api.port}/ws"
    t = ctx.auth.sign("u")
    try:
        with pytest.raises(InvalidStatus) as e:
            await websockets.connect(
                url, origin="https://evil.example", additional_headers={"Authorization": f"Bearer {t}"}
            )
        assert e.value.response.status_code == 403
        async with websockets.connect(
            url, origin="https://wall.northwind.example", additional_headers={"Authorization": f"Bearer {t}"}
        ) as ws:
            assert (await recv(ws))["type"] == "hello"
    finally:
        await api.close()
