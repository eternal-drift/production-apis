"""S1 - REST API for partner checkout integrations."""

from __future__ import annotations

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from ...context import AppContext, RunningApi, serve_asgi
from ...core.auth import SCOPE_READ, SCOPE_WRITE, Principal, bearer_from
from ...core.errors import AppError, ErrorCode, to_app_error
from ...core.http import RequestContextMiddleware, problem
from ...core.logging import get_logger
from ...domain.models import CreateOrder, Order, OrderPage, OrderStatus

log = get_logger("rest")
API = "rest"


class StatusChange(BaseModel):
    status: OrderStatus


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    requestId: str  # noqa: N815 - wire name
    errors: Any | None = None


PROBLEMS: dict[int | str, dict[str, Any]] = {
    c: {"model": Problem, "content": {"application/problem+json": {}}}
    for c in (400, 401, 403, 404, 409, 412, 429)
}


def _etag(order: Order) -> str:
    return f'"{order.version}"'


def create_rest_app(ctx: AppContext) -> FastAPI:
    s = ctx.settings
    app = FastAPI(
        title="Orders REST API",
        version="1.0.0",
        description="S1: partner checkout integration. See docs/scenarios.md.",
        docs_url=None if s.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_prod else "/openapi.json",
    )
    app.add_middleware(
        RequestContextMiddleware, rate_limit=s.rate_limit_max, window_seconds=s.rate_limit_window_seconds
    )

    # ---- Errors: every failure is problem+json, internals never leak ----
    def _rid(request: Request) -> str:
        return getattr(request.state, "request_id", "-")

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return problem(exc, instance=request.url.path, request_id=_rid(request))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()
        ]
        return problem(
            AppError(ErrorCode.VALIDATION, "Request validation failed", details),
            instance=request.url.path,
            request_id=_rid(request),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", request_id=_rid(request), error=repr(exc))
        return problem(to_app_error(exc), instance=request.url.path, request_id=_rid(request))

    # ---- Auth dependencies ----
    def authenticate(authorization: Annotated[str | None, Header()] = None) -> Principal:
        return ctx.auth.verify(bearer_from(authorization))

    def require(scope: str) -> Any:
        def dep(principal: Principal = Depends(authenticate)) -> Principal:  # noqa: B008
            principal.require(scope)
            return principal

        return Depends(dep)

    # ---- Operational endpoints (unauthenticated, rate-limit exempt) ----
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        try:
            await ctx.orders.ready()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(ctx.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    # ---- Orders ----
    @app.post(
        "/v1/orders",
        tags=["orders"],
        summary="Create an order (idempotent with Idempotency-Key)",
        status_code=201,
        response_model=Order,
        response_model_by_alias=True,
        responses=PROBLEMS,
        dependencies=[require(SCOPE_WRITE)],
    )
    async def create_order(
        body: CreateOrder,
        response: Response,
        idempotency_key: Annotated[str | None, Header(min_length=8, max_length=128)] = None,
    ) -> Order:
        order, replayed = await ctx.metrics.observe(
            API, "createOrder", lambda: ctx.orders.create(body.model_dump(by_alias=True), idempotency_key)
        )
        response.headers["etag"] = _etag(order)
        if replayed:
            response.status_code = 200
            response.headers["idempotent-replayed"] = "true"
        else:
            response.headers["location"] = f"/v1/orders/{order.id}"
        return order

    @app.get(
        "/v1/orders",
        tags=["orders"],
        summary="List orders with cursor pagination",
        response_model=OrderPage,
        response_model_by_alias=True,
        responses=PROBLEMS,
        dependencies=[require(SCOPE_READ)],
    )
    async def list_orders(
        customer_id: Annotated[str | None, Query(alias="customerId")] = None,
        status: OrderStatus | None = None,
        cursor: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> OrderPage:
        return await ctx.metrics.observe(
            API,
            "listOrders",
            lambda: ctx.orders.list(customer_id=customer_id, status=status, cursor=cursor, limit=limit),
        )

    @app.get(
        "/v1/orders/{order_id}",
        tags=["orders"],
        summary="Fetch an order (supports If-None-Match)",
        response_model=Order,
        response_model_by_alias=True,
        responses=PROBLEMS,
        dependencies=[require(SCOPE_READ)],
    )
    async def get_order(
        order_id: UUID, response: Response, if_none_match: Annotated[str | None, Header()] = None
    ) -> Response | Order:
        order = await ctx.metrics.observe(API, "getOrder", lambda: ctx.orders.get(order_id))
        etag = _etag(order)
        if if_none_match == etag:
            return Response(status_code=304, headers={"etag": etag})
        response.headers["etag"] = etag
        response.headers["cache-control"] = "private, no-cache"
        return order

    @app.patch(
        "/v1/orders/{order_id}/status",
        tags=["orders"],
        summary="Transition order status (optimistic concurrency via If-Match)",
        response_model=Order,
        response_model_by_alias=True,
        responses=PROBLEMS,
        dependencies=[require(SCOPE_WRITE)],
    )
    async def update_status(
        order_id: UUID,
        change: StatusChange,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Order:
        expected: int | None = None
        if if_match is not None:
            m = re.fullmatch(r'"(\d+)"', if_match)
            if not m:
                raise AppError(ErrorCode.VALIDATION, 'If-Match must be a quoted version, e.g. "3"')
            expected = int(m.group(1))
        order = await ctx.metrics.observe(
            API, "updateStatus", lambda: ctx.orders.transition(order_id, change.status, expected)
        )
        response.headers["etag"] = _etag(order)
        return order

    return app


async def start_rest(ctx: AppContext, port: int | None = None) -> RunningApi:
    return await serve_asgi(
        "rest", create_rest_app(ctx), ctx.settings.host, ctx.settings.rest_port if port is None else port
    )
