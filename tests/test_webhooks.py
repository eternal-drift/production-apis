"""S5 - Webhook acceptance tests."""

import json
import time

import httpx
import pytest

from production_apis.apis.webhooks.app import create_webhooks_app
from production_apis.apis.webhooks.dispatcher import WebhookDispatcher
from production_apis.apis.webhooks.receiver import create_receiver_app
from production_apis.apis.webhooks.signing import SignatureError, sign_headers, verify
from production_apis.context import serve_asgi
from production_apis.core.errors import AppError

from .conftest import make_context


@pytest.fixture
async def receiver_factory():
    running = []

    async def make(secret_holder, *, fail_first=0):
        app = create_receiver_app(secret_holder["secret"], fail_first=fail_first)
        api = await serve_asgi("receiver", app, "127.0.0.1", 0)
        running.append(api)
        return app, f"http://127.0.0.1:{api.port}/hooks/orders"

    yield make
    for api in running:
        await api.close()


@pytest.fixture
async def dispatcher(ctx):
    d = WebhookDispatcher(ctx)
    yield d
    await d.aclose()


@pytest.mark.scenario("S5.1")
async def test_subscribe_returns_secret_exactly_once(ctx, token):
    app = create_webhooks_app(ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        r = await c.post(
            "/v1/webhook-endpoints", json={"url": "http://127.0.0.1:9/hooks", "events": ["order.created"]}
        )
        assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
        listed = (await c.get("/v1/webhook-endpoints")).json()["items"]
        assert len(listed) == 1 and "secret" not in listed[0]
        bad = await c.post("/v1/webhook-endpoints", json={"url": "http://127.0.0.1:9/h", "events": ["nope"]})
        assert bad.status_code == 400
        assert (
            await c.post(
                "/v1/webhook-endpoints",
                json={"url": "http://x", "events": []},
                headers={"authorization": "Bearer bad"},
            )
        ).status_code == 401
    await app.state.dispatcher.aclose()


@pytest.mark.scenario("S5.2")
async def test_authentic_delivery_end_to_end(ctx, dispatcher, receiver_factory, sample_order):
    app = await _setup(ctx, dispatcher, receiver_factory)  # receiver verifies HMAC with the endpoint's secret
    order, _ = await ctx.orders.create(sample_order)
    await dispatcher.drain()
    processed = list(app.state.processed.values())
    assert len(processed) == 1 and app.state.rejected == 0  # signature verified by the receiver
    assert processed[0]["type"] == "order.created" and processed[0]["data"]["id"] == str(order.id)
    delivery = next(iter(dispatcher.deliveries.values()))
    assert delivery.status == "delivered" and delivery.attempts == 1


@pytest.mark.scenario("S5.3")
def test_signature_verification_rejects_tampering_replay_and_wrong_secret():
    body = b'{"id":"evt_1"}'
    headers = sign_headers("whsec_correct_secret_value", "evt_1", body)
    assert verify("whsec_correct_secret_value", headers, body) == "evt_1"
    with pytest.raises(SignatureError):
        verify("whsec_correct_secret_value", headers, b'{"id":"evt_2"}')  # tampered body
    with pytest.raises(SignatureError):
        verify("whsec_other_secret_value_xx", headers, body)  # wrong secret
    with pytest.raises(SignatureError):
        verify("whsec_correct_secret_value", headers, body, now=time.time() + 301)  # replay after tolerance
    with pytest.raises(SignatureError):
        verify("whsec_correct_secret_value", {**headers, "webhook-id": "evt_other"}, body)  # id is signed
    with pytest.raises(SignatureError):
        verify("whsec_correct_secret_value", {}, body)  # missing headers
    # secret rotation: old and new secrets are both accepted during the overlap window
    assert verify(["whsec_new_secret_value_xx", "whsec_correct_secret_value"], headers, body) == "evt_1"


async def _setup(ctx, dispatcher, receiver_factory, *, fail_first=0, wrong_secret=False):
    # The endpoint secret is only known after registration, so register against a placeholder
    # URL, then start the receiver with the real secret and point the endpoint at it.
    ep = await dispatcher.register(
        "http://127.0.0.1:9/placeholder", ["order.created", "order.status_changed"]
    )
    secret = "wrong-secret-value-xx" if wrong_secret else ep.secret
    app, url = await receiver_factory({"secret": secret}, fail_first=fail_first)
    ep.url = url
    return app


@pytest.mark.scenario("S5.4")
async def test_retries_with_same_webhook_id_until_receiver_recovers(
    ctx, dispatcher, receiver_factory, sample_order
):
    app = await _setup(ctx, dispatcher, receiver_factory, fail_first=2)
    await ctx.orders.create(sample_order)
    await dispatcher.drain()
    d = next(iter(dispatcher.deliveries.values()))
    assert d.status == "delivered" and d.attempts == 3 and app.state.requests == 3
    assert len(app.state.processed) == 1  # processed once, under the same Webhook-Id


@pytest.mark.scenario("S5.4")
async def test_permanent_failures_are_not_retried_and_exhausted_go_to_dead_letter(
    receiver_factory, sample_order
):
    # permanent 4xx (receiver rejects our signature): no retries
    ctx = make_context(rate_limit_max=1000)
    d1 = WebhookDispatcher(ctx)
    app = await _setup(ctx, d1, receiver_factory, wrong_secret=True)
    await ctx.orders.create(sample_order)
    await d1.drain()
    delivery = next(iter(d1.deliveries.values()))
    assert delivery.status == "failed" and delivery.attempts == 1 and delivery.last_status_code == 401
    await d1.aclose()

    # persistent 5xx: bounded attempts, then dead-lettered
    ctx2 = make_context(webhook_max_attempts=3)
    d2 = WebhookDispatcher(ctx2)
    app2 = await _setup(ctx2, d2, receiver_factory, fail_first=100)
    await ctx2.orders.create(sample_order)
    await d2.drain()
    assert [d.attempts for d in d2.dead_letters()] == [3] and app2.state.requests == 3
    await d2.aclose()
    assert app is not None


@pytest.mark.scenario("S5.5")
async def test_receiver_is_idempotent(receiver_factory):
    holder = {"secret": "whsec_receiver_secret_value"}
    app, url = await receiver_factory(holder)
    body = b'{"id":"evt_9","type":"order.created"}'
    async with httpx.AsyncClient() as c:
        for _ in range(2):
            r = await c.post(url, content=body, headers=sign_headers(holder["secret"], "evt_9", body))
            assert r.status_code == 200
    assert len(app.state.processed) == 1 and app.state.duplicates == 1
    async with httpx.AsyncClient() as c:
        r = await c.post(
            url,
            content=body,
            headers={
                "webhook-id": "evt_9",
                "webhook-timestamp": str(int(time.time())),
                "webhook-signature": "v1=bad",
            },
        )
        assert r.status_code == 401


@pytest.mark.scenario("S5.6")
@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data",
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://10.0.0.5/hook",
        "https://[::1]/hook",
        "http://example.com/hook",  # not https
        "https://user:pass@example.com/hook",
    ],
)
async def test_ssrf_targets_are_rejected_in_production_mode(url):
    ctx = make_context(webhook_allow_private_targets=False)
    d = WebhookDispatcher(ctx)
    with pytest.raises(AppError) as e:
        await d.register(url, ["order.created"])
    assert e.value.code.value == "VALIDATION"
    await d.aclose()


def test_payload_is_stable_json_signed_over_raw_bytes():
    body = json.dumps({"a": 1}, separators=(",", ":")).encode()
    h = sign_headers("whsec_stable_secret_value", "evt", body, now=1_700_000_000)
    assert h["webhook-signature"].startswith("v1=") and h["webhook-timestamp"] == "1700000000"
