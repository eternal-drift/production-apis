from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from production_apis.context import AppContext, create_context
from production_apis.core.config import load_settings
from production_apis.core.logging import configure_logging

configure_logging(quiet=True)

SAMPLE_ORDER = {
    "customerId": "cust_1",
    "currency": "USD",
    "items": [
        {"sku": "SKU-1", "quantity": 2, "unitPriceCents": 1500},
        {"sku": "SKU-2", "quantity": 1, "unitPriceCents": 999},
    ],
}


def make_context(**overrides: object) -> AppContext:
    defaults: dict[str, object] = {
        "APP_ENV": "test",
        "host": "127.0.0.1",
        "webhook_backoff_base_seconds": 0.01,
    }
    return create_context(load_settings(**{**defaults, **overrides}))


@pytest.fixture
def ctx() -> AppContext:
    return make_context(rate_limit_max=10_000)


@pytest.fixture
def token(ctx: AppContext) -> str:
    return ctx.auth.sign("test-user")


@pytest.fixture
def read_only_token(ctx: AppContext) -> str:
    return ctx.auth.sign("reader", ["orders:read"])


@pytest.fixture
def sample_order() -> dict[str, object]:
    return SAMPLE_ORDER


async def wait_for(predicate: Callable[[], object], timeout: float = 3.0) -> object:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if value := predicate():
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("wait_for timed out")
        await asyncio.sleep(0.01)
