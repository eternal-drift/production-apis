"""S3 - gRPC API for internal service-to-service calls (Fulfillment, Payments)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc
from google.protobuf.message import Message
from grpc import aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from ...context import AppContext, RunningApi
from ...core.auth import SCOPE_READ, SCOPE_WRITE
from ...core.errors import AppError, ErrorCode, to_app_error
from ...core.logging import get_logger
from ...domain import models as m
from ...domain.events import OrderEvent
from ...gen.orders.v1 import orders_pb2 as pb
from ...gen.orders.v1 import orders_pb2_grpc as pb_grpc

log = get_logger("grpc")
API = "grpc"

GRPC_CODE: dict[ErrorCode, grpc.StatusCode] = {
    ErrorCode.VALIDATION: grpc.StatusCode.INVALID_ARGUMENT,
    ErrorCode.UNAUTHENTICATED: grpc.StatusCode.UNAUTHENTICATED,
    ErrorCode.FORBIDDEN: grpc.StatusCode.PERMISSION_DENIED,
    ErrorCode.NOT_FOUND: grpc.StatusCode.NOT_FOUND,
    ErrorCode.CONFLICT: grpc.StatusCode.ALREADY_EXISTS,
    ErrorCode.PRECONDITION_FAILED: grpc.StatusCode.FAILED_PRECONDITION,
    ErrorCode.RATE_LIMITED: grpc.StatusCode.RESOURCE_EXHAUSTED,
    ErrorCode.INTERNAL: grpc.StatusCode.INTERNAL,
}

# Required scope per fully-qualified method.
METHOD_SCOPES = {
    "/orders.v1.OrderService/CreateOrder": SCOPE_WRITE,
    "/orders.v1.OrderService/UpdateOrderStatus": SCOPE_WRITE,
    "/orders.v1.OrderService/GetOrder": SCOPE_READ,
    "/orders.v1.OrderService/ListOrders": SCOPE_READ,
    "/orders.v1.OrderService/WatchOrders": SCOPE_READ,
}


# ---- mapping between domain objects and protobuf messages ----
def to_pb_order(o: m.Order) -> pb.Order:
    return pb.Order(
        id=str(o.id),
        customer_id=o.customer_id,
        currency=o.currency,
        items=[
            pb.OrderItem(sku=i.sku, quantity=i.quantity, unit_price_cents=i.unit_price_cents) for i in o.items
        ],
        total_cents=o.total_cents,
        status=pb.OrderStatus.Value(f"ORDER_STATUS_{o.status.value}"),
        version=o.version,
        created_at=o.created_at.isoformat(),
        updated_at=o.updated_at.isoformat(),
    )


def from_pb_status(value: int) -> m.OrderStatus | None:
    if value == pb.ORDER_STATUS_UNSPECIFIED:
        return None
    return m.OrderStatus(pb.OrderStatus.Name(value).removeprefix("ORDER_STATUS_"))


def to_pb_event(e: OrderEvent) -> pb.OrderEvent:
    return pb.OrderEvent(id=e.id, type=e.type, occurred_at=e.occurred_at, order=to_pb_order(e.data))


class OrderServicer(pb_grpc.OrderServiceServicer):
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def CreateOrder(self, request: pb.CreateOrderRequest, context: aio.ServicerContext) -> pb.Order:  # noqa: N802
        payload = {
            "customerId": request.customer_id,
            "currency": request.currency or "USD",
            "items": [
                {"sku": i.sku, "quantity": i.quantity, "unitPriceCents": i.unit_price_cents}
                for i in request.items
            ],
        }
        order, _ = await self.ctx.orders.create(payload, request.idempotency_key or None)
        return to_pb_order(order)

    async def GetOrder(self, request: pb.GetOrderRequest, context: aio.ServicerContext) -> pb.Order:  # noqa: N802
        return to_pb_order(await self.ctx.orders.get(request.id))

    async def ListOrders(
        self, request: pb.ListOrdersRequest, context: aio.ServicerContext
    ) -> pb.ListOrdersResponse:  # noqa: N802
        page = await self.ctx.orders.list(
            customer_id=request.customer_id or None,
            status=from_pb_status(request.status),
            cursor=request.page_token or None,
            limit=request.page_size or 20,
        )
        return pb.ListOrdersResponse(
            orders=[to_pb_order(o) for o in page.items], next_page_token=page.next_cursor or ""
        )

    async def UpdateOrderStatus(
        self, request: pb.UpdateOrderStatusRequest, context: aio.ServicerContext
    ) -> pb.Order:  # noqa: N802
        target = from_pb_status(request.status)
        if target is None:
            raise AppError(ErrorCode.VALIDATION, "status is required")
        order = await self.ctx.orders.transition(request.id, target, request.expected_version or None)
        return to_pb_order(order)

    async def WatchOrders(  # noqa: N802
        self, request: pb.WatchOrdersRequest, context: aio.ServicerContext
    ) -> AsyncIterator[pb.OrderEvent]:
        cid = request.customer_id
        self.ctx.metrics.connections.labels(API).inc()
        try:
            # Async generators give natural flow control: the next event is only pulled
            # once HTTP/2 has accepted the previous one. Cancellation closes the stream.
            async for e in self.ctx.orders.events.stream(lambda e: not cid or e.data.customer_id == cid):
                yield to_pb_event(e)
        finally:
            self.ctx.metrics.connections.labels(API).dec()


class AuthInterceptor(aio.ServerInterceptor):
    """Authenticates ``authorization`` metadata, enforces per-method scopes, records metrics and maps errors."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def intercept_service(
        self, continuation: Callable[[Any], Any], details: grpc.HandlerCallDetails
    ) -> Any:
        handler = await continuation(details)
        if handler is None or details.method.startswith("/grpc.health."):
            return handler  # health checks stay unauthenticated for load balancers
        method = details.method
        scope = METHOD_SCOPES.get(method)

        def check(context: aio.ServicerContext) -> None:
            md = dict(context.invocation_metadata() or ())
            raw = md.get("authorization", "")
            token = raw.split(" ", 1)[1] if raw.lower().startswith("bearer ") else None
            principal = self.ctx.auth.verify(token)
            if scope is None:
                raise AppError(ErrorCode.FORBIDDEN, "Unknown method")  # deny by default
            principal.require(scope)

        async def fail(context: aio.ServicerContext, err: BaseException) -> None:
            app_err = to_app_error(err)
            if app_err.code == ErrorCode.INTERNAL:
                log.error("grpc_handler_failure", method=method, error=repr(err))
            context.set_trailing_metadata((("error-code", app_err.code.value),))
            await context.abort(GRPC_CODE[app_err.code], app_err.message)

        op = method.rsplit("/", 1)[-1]

        if handler.unary_unary:
            inner = handler.unary_unary

            async def unary(request: Message, context: aio.ServicerContext) -> Any:
                try:
                    check(context)
                    return await self.ctx.metrics.observe(API, op, lambda: inner(request, context))
                except AppError as err:
                    await fail(context, err)
                except (aio.AbortError, asyncio.CancelledError):
                    raise
                except Exception as err:  # noqa: BLE001 - last line of defence, mapped to INTERNAL
                    await fail(context, err)

            return grpc.unary_unary_rpc_method_handler(
                unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.unary_stream:
            inner_stream = handler.unary_stream

            async def stream(request: Message, context: aio.ServicerContext) -> AsyncIterator[Any]:
                try:
                    check(context)
                except AppError as err:
                    await fail(context, err)
                    return
                async for item in inner_stream(request, context):
                    yield item

            return grpc.unary_stream_rpc_method_handler(
                stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        return handler


async def start_grpc(ctx: AppContext, port: int | None = None) -> RunningApi:
    s = ctx.settings
    server = aio.server(
        interceptors=[AuthInterceptor(ctx)],
        options=[
            ("grpc.max_receive_message_length", 1024 * 1024),
            ("grpc.max_send_message_length", 4 * 1024 * 1024),
            ("grpc.keepalive_time_ms", 60_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.max_connection_age_ms", 30 * 60_000),  # periodic reconnect so load rebalances
            ("grpc.max_connection_age_grace_ms", 30_000),
        ],
        maximum_concurrent_rpcs=1000,  # shed load with RESOURCE_EXHAUSTED instead of collapsing
    )
    pb_grpc.add_OrderServiceServicer_to_server(OrderServicer(ctx), server)
    health_servicer = health.aio.HealthServicer() if hasattr(health, "aio") else health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set("orders.v1.OrderService", health_pb2.HealthCheckResponse.SERVING)

    bound = server.add_insecure_port(
        f"{s.host}:{s.grpc_port if port is None else port}"
    )  # TLS terminates at the mesh/ingress
    await server.start()

    async def close() -> None:
        await health_servicer.enter_graceful_shutdown()  # flip to NOT_SERVING so LBs drain first
        await server.stop(grace=5)

    return RunningApi("grpc", bound, close)
