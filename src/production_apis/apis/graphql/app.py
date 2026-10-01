"""S2 - GraphQL API for the mobile "My Orders" experience."""

from __future__ import annotations

import enum
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, TypeVar

import strawberry
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from graphql import GraphQLError
from strawberry.dataloader import DataLoader
from strawberry.exceptions import ConnectionRejectionError
from strawberry.extensions import DisableIntrospection, MaskErrors, QueryDepthLimiter
from strawberry.fastapi import BaseContext, GraphQLRouter
from strawberry.types import Info

from ...context import AppContext, RunningApi, serve_asgi
from ...core.auth import SCOPE_READ, SCOPE_WRITE, Principal, bearer_from
from ...core.errors import HTTP_STATUS, AppError, ErrorCode
from ...core.http import RequestContextMiddleware
from ...core.logging import get_logger
from ...domain import models as m

log = get_logger("graphql")
API = "graphql"
T = TypeVar("T")

OrderStatusGQL = strawberry.enum(
    enum.Enum("OrderStatus", {s.value: s.value for s in m.OrderStatus}), name="OrderStatus"
)
OrderEventTypeGQL = strawberry.enum(
    enum.Enum(
        "OrderEventType", {"ORDER_CREATED": "order.created", "ORDER_STATUS_CHANGED": "order.status_changed"}
    ),
    name="OrderEventType",
)


@strawberry.type(name="OrderItem")
class OrderItemType:
    sku: str
    quantity: int
    unit_price_cents: int


@strawberry.type(name="Order")
class OrderType:
    id: strawberry.ID
    currency: str
    total_cents: int
    status: OrderStatusGQL  # type: ignore[valid-type]
    version: int
    created_at: str
    updated_at: str
    items: list[OrderItemType]
    _customer_id: strawberry.Private[str]

    @strawberry.field
    def customer(self) -> CustomerType:
        return CustomerType(id=strawberry.ID(self._customer_id))

    @staticmethod
    def of(o: m.Order) -> OrderType:
        return OrderType(
            id=strawberry.ID(str(o.id)),
            currency=o.currency,
            total_cents=o.total_cents,
            status=OrderStatusGQL(o.status.value),
            version=o.version,
            created_at=o.created_at.isoformat(),
            updated_at=o.updated_at.isoformat(),
            items=[
                OrderItemType(sku=i.sku, quantity=i.quantity, unit_price_cents=i.unit_price_cents)
                for i in o.items
            ],
            _customer_id=o.customer_id,
        )


@strawberry.type(
    name="Customer", description="Resolved through a DataLoader: N orders cost one repository call, not N."
)
class CustomerType:
    id: strawberry.ID

    @strawberry.field
    async def orders(self, info: Info, first: int = 10) -> list[OrderType]:
        ctx: GqlContext = info.context
        principal(info).require(SCOPE_READ)
        found = await ctx.customer_orders.load(str(self.id))
        return [OrderType.of(o) for o in found[: max(1, min(first, 100))]]


@strawberry.type(name="OrderEdge")
class OrderEdge:
    cursor: str
    node: OrderType


@strawberry.type(name="OrderConnection")
class OrderConnection:
    edges: list[OrderEdge]
    next_cursor: str | None


@strawberry.type(name="OrderEvent")
class OrderEventType:
    id: strawberry.ID
    type: OrderEventTypeGQL  # type: ignore[valid-type]
    occurred_at: str
    order: OrderType


@strawberry.input(name="OrderItemInput")
class OrderItemInput:
    sku: str
    quantity: int
    unit_price_cents: int


@strawberry.input(name="CreateOrderInput")
class CreateOrderInput:
    customer_id: str
    items: list[OrderItemInput]
    currency: str = "USD"
    idempotency_key: str | None = strawberry.field(
        default=None, description="Client-generated key making the mutation safe to retry."
    )


class GqlContext(BaseContext):
    """Per-request context. A new DataLoader per request batches and caches within that request only."""

    def __init__(
        self, app: AppContext, principal: Principal | None, customer_orders: DataLoader[str, list[m.Order]]
    ) -> None:
        super().__init__()
        self.app = app
        self.principal = principal
        self.customer_orders = customer_orders


def principal(info: Info) -> Principal:
    p = info.context.principal
    if p is None:
        raise GraphQLError("Missing bearer token", extensions={"code": ErrorCode.UNAUTHENTICATED.value})
    return p  # type: ignore[no-any-return]


async def _run(info: Info, operation: str, scope: str, fn: Callable[[AppContext], Awaitable[T]]) -> T:
    """Authorize, time, and translate AppError into a GraphQL error carrying ``extensions.code``."""
    app: AppContext = info.context.app
    try:
        principal(info).require(scope)
        return await app.metrics.observe(API, operation, lambda: fn(app))
    except AppError as err:
        extensions: dict[str, Any] = {"code": err.code.value}
        if err.details:
            extensions["details"] = err.details
        raise GraphQLError(err.message, extensions=extensions) from err


@strawberry.type
class Query:
    @strawberry.field
    async def order(self, info: Info, id: strawberry.ID) -> OrderType | None:
        async def go(app: AppContext) -> OrderType | None:
            try:
                return OrderType.of(await app.orders.get(str(id)))
            except AppError as e:
                if e.code == ErrorCode.NOT_FOUND:
                    return None
                raise

        return await _run(info, "order", SCOPE_READ, go)

    @strawberry.field
    async def orders(
        self,
        info: Info,
        customer_id: str | None = None,
        status: OrderStatusGQL | None = None,  # type: ignore[valid-type]
        first: int = 20,
        after: str | None = None,
    ) -> OrderConnection:
        async def go(app: AppContext) -> OrderConnection:
            page = await app.orders.list(
                customer_id=customer_id,
                status=m.OrderStatus(status.value) if status else None,
                limit=first,
                cursor=after,
            )
            return OrderConnection(
                edges=[OrderEdge(cursor=str(o.id), node=OrderType.of(o)) for o in page.items],
                next_cursor=page.next_cursor,
            )

        return await _run(info, "orders", SCOPE_READ, go)


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_order(self, info: Info, input: CreateOrderInput) -> OrderType:
        async def go(app: AppContext) -> OrderType:
            payload = {
                "customerId": input.customer_id,
                "currency": input.currency,
                "items": [
                    {"sku": i.sku, "quantity": i.quantity, "unitPriceCents": i.unit_price_cents}
                    for i in input.items
                ],
            }
            order, _ = await app.orders.create(payload, input.idempotency_key)
            return OrderType.of(order)

        return await _run(info, "createOrder", SCOPE_WRITE, go)

    @strawberry.mutation
    async def update_order_status(
        self,
        info: Info,
        id: strawberry.ID,
        status: OrderStatusGQL,  # type: ignore[valid-type]
        expected_version: int | None = None,
    ) -> OrderType:
        async def go(app: AppContext) -> OrderType:
            return OrderType.of(
                await app.orders.transition(str(id), m.OrderStatus(status.value), expected_version)
            )

        return await _run(info, "updateOrderStatus", SCOPE_WRITE, go)


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def order_events(
        self, info: Info, customer_id: str | None = None
    ) -> AsyncGenerator[OrderEventType, None]:
        app: AppContext = info.context.app
        principal(info).require(SCOPE_READ)
        app.metrics.connections.labels(API).inc()
        try:
            async for e in app.orders.events.stream(
                lambda e: customer_id is None or e.data.customer_id == customer_id
            ):
                yield OrderEventType(
                    id=strawberry.ID(e.id),
                    type=OrderEventTypeGQL(e.type),
                    occurred_at=e.occurred_at,
                    order=OrderType.of(e.data),
                )
        finally:
            app.metrics.connections.labels(API).dec()


def build_schema(ctx: AppContext) -> strawberry.Schema:
    s = ctx.settings
    extensions: list[Any] = [
        QueryDepthLimiter(max_depth=s.graphql_max_depth),
        # Hide internals: only GraphQLErrors we raised intentionally (with extensions.code) pass through.
        MaskErrors(
            should_mask_error=lambda e: (
                not (isinstance(e, GraphQLError) and e.extensions and "code" in e.extensions)
                and e.original_error is not None
                and not isinstance(e.original_error, GraphQLError)
            )
        ),
    ]
    if not s.introspection_enabled:
        extensions.append(DisableIntrospection)
    return strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription, extensions=extensions)


def create_graphql_app(ctx: AppContext) -> FastAPI:
    s = ctx.settings
    app = FastAPI(title="Orders GraphQL API", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        RequestContextMiddleware, rate_limit=s.rate_limit_max, window_seconds=s.rate_limit_window_seconds
    )

    @app.exception_handler(AppError)
    async def _gql_http_error(_: Request, exc: AppError) -> JSONResponse:
        # GraphQL-shaped body with a real HTTP status for gateways and clients.
        return JSONResponse(
            {"errors": [{"message": exc.message, "extensions": {"code": exc.code.value}}]},
            status_code=HTTP_STATUS[exc.code],
        )

    def _make_context(authorization: str | None) -> GqlContext:
        async def load(ids: list[str]) -> list[list[m.Order]]:
            by_customer = await ctx.orders.orders_by_customers(list(ids))
            return [by_customer.get(i, []) for i in ids]

        return GqlContext(
            ctx,
            ctx.auth.verify(bearer_from(authorization)) if authorization is not None else None,
            DataLoader(load_fn=load),
        )

    async def get_context(request: Request = None, ws: WebSocket = None) -> GqlContext:  # type: ignore[assignment]
        if ws is not None:
            # WebSocket: browsers cannot set headers, so the token arrives in connection_init
            # and is verified in on_ws_connect below.
            return _make_context(None)
        # HTTP: an absent or invalid token is rejected up front with 401.
        header = request.headers.get("authorization")
        if header is None:
            raise AppError(ErrorCode.UNAUTHENTICATED, "Missing bearer token")
        return _make_context(header)

    class Router(GraphQLRouter):  # type: ignore[type-arg]
        async def on_ws_connect(self, context: GqlContext) -> Any:  # type: ignore[override]
            params = context.connection_params or {}
            try:
                context.principal = ctx.auth.verify(bearer_from(params.get("authorization")))
            except AppError as err:
                raise ConnectionRejectionError({"code": err.code.value}) from err
            return {}

    router = Router(
        build_schema(ctx),
        context_getter=get_context,
        graphql_ide="graphiql" if not s.is_prod else None,
        subscription_protocols=["graphql-transport-ws"],
    )
    app.include_router(router, prefix="/graphql")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.state.make_context = _make_context
    return app


async def start_graphql(ctx: AppContext, port: int | None = None) -> RunningApi:
    return await serve_asgi(
        "graphql",
        create_graphql_app(ctx),
        ctx.settings.host,
        ctx.settings.graphql_port if port is None else port,
    )
