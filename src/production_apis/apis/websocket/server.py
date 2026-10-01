"""S4 - WebSocket API for the live support wallboard.

Wire protocol (JSON text frames)
  client -> server: {"type":"subscribe","customerId":"C1"?}   (re)set the filter, no reconnect needed
                    {"type":"unsubscribe"}
                    {"type":"ping"}
  server -> client: {"type":"hello","subject":...}  {"type":"subscribed",...}  {"type":"event",...}
                    {"type":"pong"}  {"type":"error","code":...,"message":...}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

from ...context import AppContext, RunningApi
from ...core.auth import SCOPE_READ, Principal, bearer_from
from ...core.errors import HTTP_STATUS, AppError, ErrorCode
from ...core.logging import get_logger
from ...domain.events import OrderEvent

log = get_logger("websocket")
API = "websocket"
BEARER_PROTOCOL = Subprotocol("bearer")

OUTBOUND_QUEUE = 100  # slow consumers are disconnected rather than buffered without bound
MSG_BURST, MSG_PER_SEC = 20, 10  # inbound token bucket per connection


def token_from_request(request: Request) -> str | None:
    """Servers/CLIs send an Authorization header; browsers (no custom headers) send
    ``new WebSocket(url, ["bearer", token])`` which arrives as Sec-WebSocket-Protocol."""
    if token := bearer_from(request.headers.get("Authorization")):
        return token
    offered = [p.strip() for p in (request.headers.get("Sec-WebSocket-Protocol") or "").split(",")]
    if len(offered) == 2 and offered[0] == BEARER_PROTOCOL:
        return offered[1]
    return None


def select_subprotocol(_: ServerConnection, offered: Any) -> Subprotocol | None:
    return BEARER_PROTOCOL if BEARER_PROTOCOL in offered else None  # never echo the token back


class WallboardServer:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.connections = 0

    # Rejecting during the HTTP handshake means a bad client never gets a socket.
    def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.path == "/healthz":
            return connection.respond(HTTPStatus.OK, "ok\n")
        if request.path != "/ws":
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if self.connections >= self.ctx.settings.ws_max_connections:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "too many connections\n")
        try:
            self.ctx.auth.verify(token_from_request(request)).require(SCOPE_READ)
        except AppError as err:
            status = HTTPStatus(HTTP_STATUS[err.code])
            response = connection.respond(status, f"{err.message}\n")
            if status == HTTPStatus.UNAUTHORIZED:
                response.headers["WWW-Authenticate"] = "Bearer"
            return response
        return None

    async def handle(self, ws: ServerConnection) -> None:
        s = self.ctx.settings
        principal: Principal = self.ctx.auth.verify(token_from_request(ws.request))  # type: ignore[arg-type]
        out: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=OUTBOUND_QUEUE)
        filter_customer: str | None = None
        subscribed = False
        limited = False
        tokens, last = float(MSG_BURST), time.monotonic()

        def enqueue(msg: dict[str, Any]) -> None:
            try:
                out.put_nowait(msg)
            except asyncio.QueueFull:
                # Backpressure policy: a consumer that cannot keep up is cut off (1013 = try again later).
                asyncio.get_running_loop().create_task(ws.close(1013, "slow consumer"))

        def on_event(e: OrderEvent) -> None:
            if subscribed and (filter_customer is None or e.data.customer_id == filter_customer):
                enqueue(
                    {
                        "type": "event",
                        "id": e.id,
                        "event": e.type,
                        "occurredAt": e.occurred_at,
                        "order": e.data.model_dump(mode="json", by_alias=True),
                    }
                )

        async def writer() -> None:
            while True:
                await ws.send(json.dumps(await out.get()))

        self.connections += 1
        self.ctx.metrics.connections.labels(API).inc()
        off = self.ctx.orders.events.subscribe(on_event)
        writer_task = asyncio.create_task(writer())
        enqueue({"type": "hello", "subject": principal.subject})
        try:
            async for raw in ws:
                now = time.monotonic()
                tokens = min(MSG_BURST, tokens + (now - last) * MSG_PER_SEC)
                last = now
                if limited:
                    continue  # keep draining so the close handshake completes cleanly (no TCP reset)
                if tokens < 1:
                    limited = True
                    writer_task.cancel()  # stop sending before closing so the close frame is not starved
                    _close_task = asyncio.create_task(ws.close(1008, "rate limit exceeded"))
                    continue
                tokens -= 1
                try:
                    msg = json.loads(raw)
                    kind = msg["type"]
                except (ValueError, KeyError, TypeError):
                    enqueue(
                        {"type": "error", "code": ErrorCode.VALIDATION.value, "message": "Malformed message"}
                    )
                    continue
                if kind == "subscribe":
                    cid = msg.get("customerId")
                    if cid is not None and not isinstance(cid, str):
                        enqueue(
                            {
                                "type": "error",
                                "code": ErrorCode.VALIDATION.value,
                                "message": "customerId must be a string",
                            }
                        )
                        continue
                    filter_customer, subscribed = cid, True
                    enqueue({"type": "subscribed", "customerId": cid})
                elif kind == "unsubscribe":
                    subscribed = False
                    enqueue({"type": "unsubscribed"})
                elif kind == "ping":
                    enqueue({"type": "pong"})
                else:
                    enqueue(
                        {
                            "type": "error",
                            "code": ErrorCode.VALIDATION.value,
                            "message": f"Unknown message type: {kind!r}",
                        }
                    )
        except ConnectionClosed:
            pass
        finally:
            off()
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                await writer_task
            self.connections -= 1
            self.ctx.metrics.connections.labels(API).dec()
            _ = s  # settings kept for future per-connection limits


async def start_websocket(ctx: AppContext, port: int | None = None) -> RunningApi:
    s = ctx.settings
    wall = WallboardServer(ctx)
    server = await serve(
        wall.handle,
        s.host,
        s.websocket_port if port is None else port,
        process_request=wall.process_request,
        subprotocols=[BEARER_PROTOCOL],
        select_subprotocol=select_subprotocol,
        origins=[None, *s.ws_allowed_origins],  # type: ignore[list-item]  # non-browser clients (no Origin) + explicit allow-list
        max_size=s.ws_max_message_bytes,  # oversized frame -> close 1009
        ping_interval=s.ws_heartbeat_seconds,  # dead peers are detected and terminated
        ping_timeout=s.ws_heartbeat_seconds,
        close_timeout=min(10, s.ws_heartbeat_seconds),  # do not wait long on peers that stopped responding
        compression=None,  # avoids compression-ratio (CRIME-style) and memory amplification risks
    )
    bound = next(iter(server.sockets)).getsockname()[1]

    async def close() -> None:
        server.close(close_connections=True)  # clients receive 1001 "going away" and reconnect elsewhere
        await server.wait_closed()

    return RunningApi("websocket", bound, close)
