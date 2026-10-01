"""J1 - one customer purchase touching all six APIs (see docs/scenarios.md).

Used by scripts/demo.py (human-readable narration) and tests/test_journey.py (assertions).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import websockets
from grpc import aio

from .apis.webhooks.receiver import create_receiver_app
from .context import AppContext, serve_asgi
from .gen.orders.v1 import orders_pb2 as pb
from .gen.orders.v1 import orders_pb2_grpc as pb_grpc
from .main import start_all

Say = Callable[[str], None]


async def run_journey(ctx: AppContext, say: Say = lambda _m: None) -> dict[str, Any]:
    host = ctx.settings.host
    apis = {a.name: a for a in await start_all(ctx, port_override=0)}
    receiver_app = create_receiver_app("placeholder-until-issued", fail_first=1)  # first delivery gets a 503
    receiver = await serve_asgi("receiver", receiver_app, host, 0)
    token = ctx.auth.sign("sam-mobile-app")
    auth = {"authorization": f"Bearer {token}"}
    out: dict[str, Any] = {}
    cleanup = [receiver.close, *[a.close for a in apis.values()]]

    try:
        # --- Setup: Tom registers the accounting webhook (S5.1) --------------------------------
        async with httpx.AsyncClient() as http:
            reg = await http.post(
                f"http://{host}:{apis['webhooks'].port}/v1/webhook-endpoints",
                json={
                    "url": f"http://{host}:{receiver.port}/hooks/orders",
                    "events": ["order.created", "order.status_changed"],
                },
                headers=auth,
            )
            receiver_app.state.secret = reg.json()["secret"]
            say(f"[setup]    accounting webhook registered (secret shown once: {reg.json()['secret'][:10]}…)")

            # Elena's wallboard (WebSocket) and Fulfillment's watcher (gRPC stream) connect.
            wall = await websockets.connect(
                f"ws://{host}:{apis['websocket'].port}/ws", additional_headers=auth
            )
            await wall.recv()  # hello
            await wall.send(json.dumps({"type": "subscribe"}))
            await wall.recv()  # subscribed
            channel = aio.insecure_channel(f"{host}:{apis['grpc'].port}")
            cleanup.append(channel.close)
            stub = pb_grpc.OrderServiceStub(channel)
            md = (("authorization", f"Bearer {token}"),)
            watch = stub.WatchOrders(pb.WatchOrdersRequest(), metadata=md)
            await asyncio.sleep(0.3)

            # --- 1. GraphQL: Sam places the order in the mobile app ---------------------------------
            gql = await http.post(
                f"http://{host}:{apis['graphql'].port}/graphql",
                json={
                    "query": "mutation($i: CreateOrderInput!){ createOrder(input:$i){ id status totalCents } }",
                    "variables": {
                        "i": {
                            "customerId": "sam",
                            "idempotencyKey": "sam-tent-0001",
                            "items": [{"sku": "TENT-2P", "quantity": 1, "unitPriceCents": 24900}],
                        }
                    },
                },
                headers=auth,
            )
            order = gql.json()["data"]["createOrder"]
            out["graphql_order"] = order
            say(
                f"[GraphQL]  Sam's app created order {order['id'][:8]}… status={order['status']} total={order['totalCents']}c"
            )

            # --- 2. WebSocket: the wallboard flashes immediately -------------------------------------
            wall_msg = json.loads(await asyncio.wait_for(wall.recv(), 3))
            out["wallboard"] = wall_msg
            say(f"[WebSocket] wallboard received {wall_msg['event']} for {wall_msg['order']['id'][:8]}…")

            # --- 3. gRPC: Payments marks PAID; Fulfillment's stream sees it, then ships -------------
            created_evt = await asyncio.wait_for(watch.read(), 3)
            paid = await stub.UpdateOrderStatus(
                pb.UpdateOrderStatusRequest(id=order["id"], status=pb.ORDER_STATUS_PAID, expected_version=1),
                metadata=md,
            )
            paid_evt = await asyncio.wait_for(watch.read(), 3)
            out["grpc_events"] = [created_evt.type, paid_evt.type]
            say(
                f"[gRPC]     Payments set PAID (version {paid.version}); Fulfillment stream saw {paid_evt.type} → {pb.OrderStatus.Name(paid_evt.order.status)}"
            )
            shipped = await stub.UpdateOrderStatus(
                pb.UpdateOrderStatusRequest(id=order["id"], status=pb.ORDER_STATUS_SHIPPED), metadata=md
            )
            say(f"[gRPC]     Fulfillment shipped it (version {shipped.version})")
            watch.cancel()

            # --- 4. Webhooks: accounting received signed events (first attempt failed with 503) -----
            deadline = asyncio.get_running_loop().time() + 5
            while len(receiver_app.state.processed) < 3 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            out["webhook_processed"] = [p["type"] for p in receiver_app.state.processed.values()]
            out["webhook_requests"] = receiver_app.state.requests
            say(
                f"[Webhooks] accounting processed {len(receiver_app.state.processed)} signed events in {receiver_app.state.requests} requests (1 retried after 503)"
            )

            # --- 5. SOAP: the wholesale ERP reconciles -----------------------------------------------
            envelope = (
                '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tns="urn:northwind:orders:v1">'
                f"<soapenv:Body><tns:GetOrderRequest><tns:id>{order['id']}</tns:id></tns:GetOrderRequest></soapenv:Body></soapenv:Envelope>"
            )
            soap = await http.post(f"http://{host}:{apis['soap'].port}/soap", content=envelope, headers=auth)
            match = re.search(r"<tns:status>(\w+)</tns:status>", soap.text)
            out["soap_status"] = match.group(1) if match else None
            say(f"[SOAP]     ERP GetOrder → status={out['soap_status']}")

            # --- 6. REST: the partner storefront polls with a conditional GET ------------------------
            url = f"http://{host}:{apis['rest'].port}/v1/orders/{order['id']}"
            first = await http.get(url, headers=auth)
            second = await http.get(url, headers={**auth, "if-none-match": first.headers["etag"]})
            out["rest"] = {
                "status": first.json()["status"],
                "etag": first.headers["etag"],
                "revalidation": second.status_code,
            }
            say(
                f"[REST]     partner poll → status={out['rest']['status']} etag={out['rest']['etag']}; revalidation → {second.status_code}"
            )
            await wall.close()
        return out
    finally:
        for close in cleanup:
            await close()
