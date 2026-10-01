"""S6 - SOAP 1.1 API for legacy ERP / wholesale partners.

Hand-rolled over FastAPI on purpose: it shows exactly what a SOAP endpoint must get right
(contract publication, envelope parsing, fault semantics, XML hardening) rather than hiding it
behind a framework. The WSDL in ``wsdl/orders.wsdl`` is the contract; Zeep (an independent
client) is used in the tests to prove the two agree.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET  # noqa: S405 - used only to *build* output, never to parse input

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ...context import AppContext, RunningApi, serve_asgi
from ...core.auth import SCOPE_READ, SCOPE_WRITE, bearer_from
from ...core.errors import AppError, ErrorCode, to_app_error
from ...core.http import RequestContextMiddleware
from ...core.logging import get_logger
from ...domain.models import Order, OrderStatus

log = get_logger("soap")
API = "soap"
NS = "urn:northwind:orders:v1"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
WSDL_PATH = Path(__file__).resolve().parents[4] / "wsdl" / "orders.wsdl"
MAX_ENVELOPE_BYTES = 256 * 1024

ET.register_namespace("soap", SOAP_ENV)
ET.register_namespace("tns", NS)

# SOAP 1.1: Client = the caller's mistake, Server = ours.
FAULT_CODE: dict[ErrorCode, str] = {
    ErrorCode.RATE_LIMITED: "soap:Server",
    ErrorCode.INTERNAL: "soap:Server",
}


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(parent: ET.Element, tag: str, value: object) -> ET.Element:
    el = ET.SubElement(parent, _q(tag))
    el.text = str(value)
    return el


def _order_xml(parent: ET.Element, tag: str, o: Order) -> None:
    el = ET.SubElement(parent, _q(tag))
    _text(el, "id", o.id)
    _text(el, "customerId", o.customer_id)
    _text(el, "currency", o.currency)
    items = ET.SubElement(el, _q("items"))
    for i in o.items:
        item = ET.SubElement(items, _q("item"))
        _text(item, "sku", i.sku)
        _text(item, "quantity", i.quantity)
        _text(item, "unitPriceCents", i.unit_price_cents)
    _text(el, "totalCents", o.total_cents)
    _text(el, "status", o.status.value)
    _text(el, "version", o.version)
    _text(el, "createdAt", o.created_at.isoformat())
    _text(el, "updatedAt", o.updated_at.isoformat())


def _envelope(body_child: ET.Element) -> bytes:
    env = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    body = ET.SubElement(env, f"{{{SOAP_ENV}}}Body")
    body.append(body_child)
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(env, encoding="utf-8")


def fault_response(err: AppError) -> Response:
    """SOAP 1.1 Fault. The spec requires HTTP 500 for faults, whatever the cause."""
    fault = ET.Element(f"{{{SOAP_ENV}}}Fault")
    ET.SubElement(fault, "faultcode").text = FAULT_CODE.get(err.code, "soap:Client")
    ET.SubElement(fault, "faultstring").text = err.message
    detail = ET.SubElement(fault, "detail")
    biz = ET.SubElement(detail, _q("OrderFault"))
    _text(biz, "errorCode", err.code.value)
    _text(biz, "message", err.message)
    return Response(_envelope(fault), status_code=500, media_type="text/xml; charset=utf-8")


def _child_text(el: ET.Element, name: str) -> str | None:
    child = el.find(_q(name))
    return child.text.strip() if child is not None and child.text is not None else None


def _int(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, f"{field} must be an integer") from exc


Handler = Callable[[ET.Element], Awaitable[ET.Element]]


def create_soap_app(ctx: AppContext) -> FastAPI:
    s = ctx.settings
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        RequestContextMiddleware,
        rate_limit=s.rate_limit_max,
        window_seconds=s.rate_limit_window_seconds,
        max_body_bytes=MAX_ENVELOPE_BYTES,
    )
    wsdl_template = WSDL_PATH.read_text(encoding="utf-8")

    # ---- operations (request element -> response element) ----
    async def create_order(req: ET.Element) -> ET.Element:
        items_el = req.find(_q("items"))
        items = [
            {
                "sku": _child_text(i, "sku"),
                "quantity": _int(_child_text(i, "quantity"), "quantity"),
                "unitPriceCents": _int(_child_text(i, "unitPriceCents"), "unitPriceCents"),
            }
            for i in (items_el.findall(_q("item")) if items_el is not None else [])
        ]
        payload: dict[str, Any] = {"customerId": _child_text(req, "customerId") or "", "items": items}
        if currency := _child_text(req, "currency"):
            payload["currency"] = currency
        order, _ = await ctx.orders.create(payload, _child_text(req, "idempotencyKey"))
        out = ET.Element(_q("CreateOrderResponse"))
        _order_xml(out, "order", order)
        return out

    async def get_order(req: ET.Element) -> ET.Element:
        out = ET.Element(_q("GetOrderResponse"))
        _order_xml(out, "order", await ctx.orders.get(_child_text(req, "id") or ""))
        return out

    async def list_orders(req: ET.Element) -> ET.Element:
        status = _child_text(req, "status")
        try:
            status_enum = OrderStatus(status) if status else None
        except ValueError as exc:
            raise AppError(ErrorCode.VALIDATION, "Unknown status") from exc
        page = await ctx.orders.list(
            customer_id=_child_text(req, "customerId"),
            status=status_enum,
            limit=_int(_child_text(req, "limit"), "limit") or 20,
            cursor=_child_text(req, "cursor"),
        )
        out = ET.Element(_q("ListOrdersResponse"))
        orders_el = ET.SubElement(out, _q("orders"))
        for o in page.items:
            _order_xml(orders_el, "order", o)
        if page.next_cursor:
            _text(out, "nextCursor", page.next_cursor)
        return out

    async def update_status(req: ET.Element) -> ET.Element:
        raw = _child_text(req, "status")
        try:
            target = OrderStatus(raw or "")
        except ValueError as exc:
            raise AppError(ErrorCode.VALIDATION, "Unknown status") from exc
        order = await ctx.orders.transition(
            _child_text(req, "id") or "", target, _int(_child_text(req, "expectedVersion"), "expectedVersion")
        )
        out = ET.Element(_q("UpdateOrderStatusResponse"))
        _order_xml(out, "order", order)
        return out

    operations: dict[str, tuple[str, Handler]] = {
        "CreateOrderRequest": (SCOPE_WRITE, create_order),
        "GetOrderRequest": (SCOPE_READ, get_order),
        "ListOrdersRequest": (SCOPE_READ, list_orders),
        "UpdateOrderStatusRequest": (SCOPE_WRITE, update_status),
    }

    def parse_envelope(raw: bytes) -> ET.Element:
        """Parse with defusedxml: DTDs, external entities and entity expansion are all refused."""
        try:
            root = SafeET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        except (DefusedXmlException, ET.ParseError) as exc:
            raise AppError(ErrorCode.VALIDATION, "Malformed or forbidden XML") from exc
        if root.tag != f"{{{SOAP_ENV}}}Envelope":
            raise AppError(ErrorCode.VALIDATION, "Expected a SOAP 1.1 Envelope")
        body = root.find(f"{{{SOAP_ENV}}}Body")
        if body is None or len(body) != 1:
            raise AppError(ErrorCode.VALIDATION, "SOAP Body must contain exactly one element")
        return body[0]

    @app.get("/soap")
    async def wsdl(request: Request) -> Response:
        # Publish the WSDL with this request's externally visible address as the endpoint.
        if "wsdl" not in request.query_params:
            return Response("Append ?wsdl to fetch the service contract.\n", media_type="text/plain")
        location = f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/soap"
        return Response(
            wsdl_template.replace("http://localhost:3006/soap", location),
            media_type="text/xml; charset=utf-8",
        )

    @app.post("/soap")
    async def endpoint(request: Request) -> Response:
        started = time.perf_counter()
        op = "unknown"
        try:
            principal = ctx.auth.verify(bearer_from(request.headers.get("authorization")))
            raw = await request.body()
            if len(raw) > MAX_ENVELOPE_BYTES:
                raise AppError(ErrorCode.VALIDATION, "Envelope too large")
            element = parse_envelope(raw)
            op = _local(element.tag)
            if element.tag.partition("}")[0].lstrip("{") != NS or op not in operations:
                raise AppError(ErrorCode.VALIDATION, f"Unknown operation: {op}")
            scope, handler = operations[op]
            principal.require(scope)
            result = await ctx.metrics.observe(API, op.removesuffix("Request"), lambda: handler(element))
            return Response(_envelope(result), media_type="text/xml; charset=utf-8")
        except Exception as exc:
            err = to_app_error(exc)
            if err.code == ErrorCode.INTERNAL:
                log.error("soap_handler_failure", operation=op, error=repr(exc))
            if op == "unknown":  # failed before dispatch (auth / parse): not covered by metrics.observe
                ctx.metrics.requests.labels(API, op, err.code.value.lower()).inc()
                ctx.metrics.duration.labels(API, op).observe(time.perf_counter() - started)
            return fault_response(err)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(ctx.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app


async def start_soap(ctx: AppContext, port: int | None = None) -> RunningApi:
    return await serve_asgi(
        "soap", create_soap_app(ctx), ctx.settings.host, ctx.settings.soap_port if port is None else port
    )
