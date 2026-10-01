"""Dependency container handed to every API module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import uvicorn

from .core.auth import Authenticator
from .core.config import Settings
from .core.metrics import Metrics
from .domain.events import EventBus
from .domain.repository import InMemoryOrderRepository
from .domain.service import OrderService


@dataclass
class AppContext:
    settings: Settings
    metrics: Metrics
    auth: Authenticator
    orders: OrderService


def create_context(settings: Settings) -> AppContext:
    return AppContext(
        settings=settings,
        metrics=Metrics(),
        auth=Authenticator(settings),
        orders=OrderService(InMemoryOrderRepository(), EventBus()),
    )


@dataclass
class RunningApi:
    name: str
    port: int
    close: Callable[[], Awaitable[None]]


ApiStarter = Callable[..., Awaitable[RunningApi]]
"""``async def start(ctx, port=None) -> RunningApi``; port 0 picks a free port (used by tests)."""


async def serve_asgi(name: str, app: Any, host: str, port: int, *, log_level: str = "warning") -> RunningApi:
    """Run an ASGI app on uvicorn inside the current event loop and return a handle to stop it."""
    import asyncio

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=5,
        ws_max_size=1024 * 1024,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
            raise RuntimeError(f"{name} failed to start")
        await asyncio.sleep(0.01)
    bound = server.servers[0].sockets[0].getsockname()[1]

    async def close() -> None:
        server.should_exit = True
        await task

    return RunningApi(name, bound, close)
