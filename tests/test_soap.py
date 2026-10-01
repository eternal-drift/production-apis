"""S6 - SOAP acceptance tests (Zeep is an independent client that consumes our WSDL)."""

import asyncio

import httpx
import pytest
import zeep
from zeep.exceptions import Fault
from zeep.transports import Transport

from production_apis.apis.soap.app import start_soap


@pytest.fixture
async def soap_env(ctx, token):
    api = await start_soap(ctx, 0)
    base = f"http://127.0.0.1:{api.port}/soap"
    yield ctx, base, token
    await api.close()


def make_client(base, token):
    import requests

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    return zeep.Client(f"{base}?wsdl", transport=Transport(session=session))


def envelope(inner: str) -> str:
    return f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tns="urn:northwind:orders:v1"><soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>'


ITEMS = {"item": [{"sku": "SKU-1", "quantity": 2, "unitPriceCents": 1500}]}


@pytest.mark.scenario("S6.1")
async def test_wsdl_is_published_with_the_visible_endpoint(soap_env):
    _, base, _ = soap_env
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{base}?wsdl")
    assert r.status_code == 200 and "text/xml" in r.headers["content-type"]
    assert f'location="{base}"' in r.text and "CreateOrder" in r.text


@pytest.mark.scenario("S6.2")
async def test_typed_operations_through_a_standard_client(soap_env):
    ctx, base, token = soap_env

    def run():
        svc = make_client(base, token).service
        created = svc.CreateOrder(customerId="erp-1", items=ITEMS, idempotencyKey="soap-key-0001")
        again = svc.CreateOrder(customerId="erp-1", items=ITEMS, idempotencyKey="soap-key-0001")
        fetched = svc.GetOrder(id=created.id)
        paid = svc.UpdateOrderStatus(id=created.id, status="PAID", expectedVersion=1)
        listed = svc.ListOrders(customerId="erp-1", limit=10)
        return created, again, fetched, paid, listed

    created, again, fetched, paid, listed = await asyncio.to_thread(run)
    assert created.totalCents == 3000 and created.status == "PENDING"
    assert again.id == created.id and fetched.id == created.id
    assert paid.status == "PAID" and paid.version == 2
    assert len(listed.orders.order) == 1 and listed.nextCursor is None


@pytest.mark.scenario("S6.3")
async def test_business_errors_are_soap_faults(soap_env):
    _, base, token = soap_env

    def run():
        svc = make_client(base, token).service
        with pytest.raises(Fault) as missing:
            svc.GetOrder(id="00000000-0000-0000-0000-000000000000")
        order = svc.CreateOrder(customerId="erp-2", items=ITEMS)
        with pytest.raises(Fault) as illegal:
            svc.UpdateOrderStatus(id=order.id, status="SHIPPED")
        return missing.value, illegal.value

    missing, illegal = await asyncio.to_thread(run)
    assert missing.code.endswith("Client") and "not found" in missing.message
    from lxml import etree

    assert (
        "NOT_FOUND" in etree.tostring(missing.detail).decode()
        and "CONFLICT" in etree.tostring(illegal.detail).decode()
    )


@pytest.mark.scenario("S6.3")
async def test_fault_http_status_and_shape(soap_env):
    _, base, token = soap_env
    body = envelope("<tns:GetOrderRequest><tns:id>nope</tns:id></tns:GetOrderRequest>")
    async with httpx.AsyncClient() as c:
        r = await c.post(
            base, content=body, headers={"authorization": f"Bearer {token}", "content-type": "text/xml"}
        )
    assert (
        r.status_code == 500
        and "<faultcode>soap:Client</faultcode>" in r.text
        and "errorCode>VALIDATION<" in r.text
    )


@pytest.mark.scenario("S6.4")
@pytest.mark.parametrize(
    "payload",
    [
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        + envelope("<tns:GetOrderRequest><tns:id>&xxe;</tns:id></tns:GetOrderRequest>"),
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        + envelope("<tns:GetOrderRequest><tns:id>&lol2;</tns:id></tns:GetOrderRequest>"),
        "<not-xml",
        "<root/>",
    ],
)
async def test_hostile_or_malformed_xml_is_rejected_never_expanded(soap_env, payload):
    _, base, token = soap_env
    async with httpx.AsyncClient() as c:
        r = await c.post(base, content=payload, headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 500 and "errorCode>VALIDATION<" in r.text
    assert "root:" not in r.text  # /etc/passwd contents never leak


@pytest.mark.scenario("S6.4")
async def test_oversized_envelope_rejected(soap_env):
    _, base, token = soap_env
    async with httpx.AsyncClient() as c:
        r = await c.post(base, content=b"x" * 300_000, headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 413


@pytest.mark.scenario("S6.5")
async def test_authentication_and_scopes(soap_env, read_only_token):
    ctx, base, _ = soap_env
    body = envelope(
        "<tns:CreateOrderRequest><tns:customerId>c</tns:customerId><tns:items><tns:item><tns:sku>s</tns:sku><tns:quantity>1</tns:quantity><tns:unitPriceCents>1</tns:unitPriceCents></tns:item></tns:items></tns:CreateOrderRequest>"
    )
    async with httpx.AsyncClient() as c:
        anon = await c.post(base, content=body)
        ro = await c.post(base, content=body, headers={"authorization": f"Bearer {read_only_token}"})
    assert "errorCode>UNAUTHENTICATED<" in anon.text
    assert "errorCode>FORBIDDEN<" in ro.text
