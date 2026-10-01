"""S5 - Webhook management API + outbound dispatcher lifecycle."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, HttpUrl

from ...context import AppContext, RunningApi, serve_asgi
from ...core.auth import SCOPE_READ, SCOPE_WRITE, Principal, bearer_from
from ...core.errors import AppError, ErrorCode, to_app_error
from ...core.http import RequestContextMiddleware, problem
from .dispatcher import WebhookDispatcher, endpoint_view


class EndpointIn(BaseModel):
    url: HttpUrl
    events: list[str]


def create_webhooks_app(ctx: AppContext, dispatcher: WebhookDispatcher | None = None) -> FastAPI:
    s = ctx.settings
    app = FastAPI(
        title="Webhook management API",
        docs_url=None if s.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_prod else "/openapi.json",
    )
    app.add_middleware(
        RequestContextMiddleware, rate_limit=s.rate_limit_max, window_seconds=s.rate_limit_window_seconds
    )
    dispatcher = dispatcher or WebhookDispatcher(ctx)
    app.state.dispatcher = dispatcher

    def rid(r: Request) -> str:
        return getattr(r.state, "request_id", "-")

    @app.exception_handler(AppError)
    async def _app_error(r: Request, exc: AppError) -> JSONResponse:
        return problem(exc, instance=r.url.path, request_id=rid(r))

    @app.exception_handler(RequestValidationError)
    async def _validation(r: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()
        ]
        return problem(
            AppError(ErrorCode.VALIDATION, "Request validation failed", details),
            instance=r.url.path,
            request_id=rid(r),
        )

    @app.exception_handler(Exception)
    async def _unhandled(r: Request, exc: Exception) -> JSONResponse:
        return problem(to_app_error(exc), instance=r.url.path, request_id=rid(r))

    def auth(scope: str):  # type: ignore[no-untyped-def]
        def dep(authorization: Annotated[str | None, Header()] = None) -> Principal:
            p = ctx.auth.verify(bearer_from(authorization))
            p.require(scope)
            return p

        return Depends(dep)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/webhook-endpoints", status_code=201, dependencies=[auth(SCOPE_WRITE)])
    async def create_endpoint(body: EndpointIn) -> dict[str, object]:
        ep = await dispatcher.register(str(body.url), body.events)
        return endpoint_view(ep, reveal_secret=True)

    @app.get("/v1/webhook-endpoints", dependencies=[auth(SCOPE_READ)])
    async def list_endpoints() -> dict[str, object]:
        return {"items": [endpoint_view(e) for e in dispatcher.endpoints.values()]}

    @app.delete("/v1/webhook-endpoints/{endpoint_id}", status_code=204, dependencies=[auth(SCOPE_WRITE)])
    async def delete_endpoint(endpoint_id: str) -> Response:
        if not dispatcher.remove(endpoint_id):
            raise AppError(ErrorCode.NOT_FOUND, "Endpoint not found")
        return Response(status_code=204)

    @app.get("/v1/webhook-deliveries", dependencies=[auth(SCOPE_READ)])
    async def list_deliveries(status: str | None = None) -> dict[str, object]:
        items = [vars(d) for d in dispatcher.deliveries.values() if status is None or d.status == status]
        return {"items": items}

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await dispatcher.drain(2)
        await dispatcher.aclose()

    return app


async def start_webhooks(ctx: AppContext, port: int | None = None) -> RunningApi:
    return await serve_asgi(
        "webhooks",
        create_webhooks_app(ctx),
        ctx.settings.host,
        ctx.settings.webhooks_port if port is None else port,
    )
