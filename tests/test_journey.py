"""J1 - one purchase, six consumers."""

import pytest

from production_apis.journey import run_journey


@pytest.mark.scenario("J1")
async def test_one_purchase_touches_every_api(ctx):
    r = await run_journey(ctx)
    assert r["graphql_order"]["status"] == "PENDING"
    assert r["wallboard"]["event"] == "order.created"
    assert r["grpc_events"] == ["order.created", "order.status_changed"]
    assert sorted(r["webhook_processed"]) == ["order.created", "order.status_changed", "order.status_changed"]
    assert r["webhook_requests"] == 4  # three events + one retry after the injected 503
    assert r["soap_status"] == "SHIPPED"
    assert r["rest"] == {"status": "SHIPPED", "etag": '"3"', "revalidation": 304}
