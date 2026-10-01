"""S3 - gRPC acceptance tests."""

import asyncio

import grpc
import pytest
from grpc import aio
from grpc_health.v1 import health_pb2, health_pb2_grpc

from production_apis.apis.grpc_api.server import start_grpc
from production_apis.gen.orders.v1 import orders_pb2 as pb
from production_apis.gen.orders.v1 import orders_pb2_grpc as pb_grpc


@pytest.fixture
async def grpc_env(ctx):
    api = await start_grpc(ctx, 0)
    channel = aio.insecure_channel(f"127.0.0.1:{api.port}")
    yield ctx, pb_grpc.OrderServiceStub(channel), channel
    await channel.close()
    await api.close()


def md(token):
    return (("authorization", f"Bearer {token}"),)


def new_order(sample_order, **over):
    return pb.CreateOrderRequest(
        customer_id=over.get("customer_id", sample_order["customerId"]),
        items=[
            pb.OrderItem(sku=i["sku"], quantity=i["quantity"], unit_price_cents=i["unitPriceCents"])
            for i in sample_order["items"]
        ],
        idempotency_key=over.get("key", ""),
    )


@pytest.mark.scenario("S3.4")
async def test_auth_and_error_mapping(grpc_env, token, read_only_token, sample_order):
    _, stub, _ = grpc_env
    with pytest.raises(aio.AioRpcError) as e:
        await stub.GetOrder(pb.GetOrderRequest(id="x"))
    assert e.value.code() == grpc.StatusCode.UNAUTHENTICATED
    with pytest.raises(aio.AioRpcError) as e:
        await stub.CreateOrder(new_order(sample_order), metadata=md(read_only_token))
    assert e.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(aio.AioRpcError) as e:
        await stub.GetOrder(pb.GetOrderRequest(id="00000000-0000-0000-0000-000000000000"), metadata=md(token))
    assert e.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(aio.AioRpcError) as e:
        await stub.CreateOrder(pb.CreateOrderRequest(customer_id="c"), metadata=md(token))
    assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.scenario("S3.1")
async def test_typed_create_get_list(grpc_env, token, sample_order):
    _, stub, _ = grpc_env
    created = await stub.CreateOrder(new_order(sample_order, key="grpc-key-0001"), metadata=md(token))
    assert created.total_cents == 3999 and created.status == pb.ORDER_STATUS_PENDING
    again = await stub.CreateOrder(new_order(sample_order, key="grpc-key-0001"), metadata=md(token))
    assert again.id == created.id
    assert (await stub.GetOrder(pb.GetOrderRequest(id=created.id), metadata=md(token))).id == created.id
    page = await stub.ListOrders(
        pb.ListOrdersRequest(page_size=10, status=pb.ORDER_STATUS_PENDING), metadata=md(token)
    )
    assert [o.id for o in page.orders] == [created.id] and page.next_page_token == ""


@pytest.mark.scenario("S3.2")
async def test_payments_marks_order_paid_with_version_check(grpc_env, token, sample_order):
    _, stub, _ = grpc_env
    order = await stub.CreateOrder(new_order(sample_order), metadata=md(token))
    with pytest.raises(aio.AioRpcError) as e:
        await stub.UpdateOrderStatus(
            pb.UpdateOrderStatusRequest(id=order.id, status=pb.ORDER_STATUS_PAID, expected_version=9),
            metadata=md(token),
        )
    assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    paid = await stub.UpdateOrderStatus(
        pb.UpdateOrderStatusRequest(id=order.id, status=pb.ORDER_STATUS_PAID, expected_version=1),
        metadata=md(token),
    )
    assert paid.status == pb.ORDER_STATUS_PAID and paid.version == 2


@pytest.mark.scenario("S3.3")
async def test_deadline_cancels_server_work(grpc_env, token):
    ctx, stub, _ = grpc_env
    cancelled = asyncio.Event()

    async def slow_get(_id):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    ctx.orders.get = slow_get
    with pytest.raises(aio.AioRpcError) as e:
        await stub.GetOrder(pb.GetOrderRequest(id="x"), metadata=md(token), timeout=0.2)
    assert e.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    await asyncio.wait_for(cancelled.wait(), 2)  # the server stopped working


@pytest.mark.scenario("S3.5")
async def test_watch_orders_streams_and_filters(grpc_env, token, sample_order):
    ctx, stub, _ = grpc_env
    call = stub.WatchOrders(pb.WatchOrdersRequest(customer_id="watched"), metadata=md(token))
    await asyncio.sleep(0.2)
    await ctx.orders.create({**sample_order, "customerId": "other"})
    mine, _ = await ctx.orders.create({**sample_order, "customerId": "watched"})
    event = await asyncio.wait_for(call.read(), 3)
    assert event.type == "order.created" and event.order.id == str(
        mine.id
    )  # other customer's event filtered out
    call.cancel()
    await asyncio.sleep(0.1)
    assert ctx.metrics.connections.labels("grpc")._value.get() == 0  # cancellation freed the stream


@pytest.mark.scenario("S3.6")
async def test_health_service(grpc_env):
    _, _, channel = grpc_env
    stub = health_pb2_grpc.HealthStub(channel)
    r = await stub.Check(health_pb2.HealthCheckRequest(service="orders.v1.OrderService"))
    assert r.status == health_pb2.HealthCheckResponse.SERVING
