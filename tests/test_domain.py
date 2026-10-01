import pytest

from production_apis.core.errors import AppError, ErrorCode
from production_apis.domain.models import OrderStatus


async def test_create_computes_total_and_emits_event(ctx, sample_order):
    seen = []
    ctx.orders.events.subscribe(lambda e: seen.append(e.type))
    order, replayed = await ctx.orders.create(sample_order)
    assert order.total_cents == 3999 and order.status == OrderStatus.PENDING and not replayed
    assert seen == ["order.created"]


async def test_idempotency(ctx, sample_order):
    a, _ = await ctx.orders.create(sample_order, "key-12345678")
    b, replayed = await ctx.orders.create(sample_order, "key-12345678")
    assert replayed and a.id == b.id
    with pytest.raises(AppError) as e:
        await ctx.orders.create({**sample_order, "customerId": "other"}, "key-12345678")
    assert e.value.code == ErrorCode.CONFLICT


async def test_state_machine_and_optimistic_concurrency(ctx, sample_order):
    order, _ = await ctx.orders.create(sample_order)
    with pytest.raises(AppError) as e:
        await ctx.orders.transition(order.id, OrderStatus.SHIPPED)
    assert e.value.code == ErrorCode.CONFLICT
    with pytest.raises(AppError) as e:
        await ctx.orders.transition(order.id, OrderStatus.PAID, 99)
    assert e.value.code == ErrorCode.PRECONDITION_FAILED
    assert (await ctx.orders.transition(order.id, OrderStatus.PAID, 1)).version == 2


async def test_cursor_pagination(ctx, sample_order):
    for _ in range(5):
        await ctx.orders.create(sample_order)
    p1 = await ctx.orders.list(limit=2)
    p2 = await ctx.orders.list(limit=2, cursor=p1.next_cursor)
    p3 = await ctx.orders.list(limit=2, cursor=p2.next_cursor)
    assert [len(p.items) for p in (p1, p2, p3)] == [2, 2, 1] and p3.next_cursor is None
    with pytest.raises(AppError):
        await ctx.orders.list(cursor="garbage")


async def test_validation(ctx):
    with pytest.raises(AppError) as e:
        await ctx.orders.create({"customerId": "", "items": []})
    assert e.value.code == ErrorCode.VALIDATION and e.value.details
