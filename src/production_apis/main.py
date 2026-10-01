"""Process entry point: starts every enabled API in one process sharing one domain context."""

from __future__ import annotations

import asyncio
import signal

from .apis.graphql.app import start_graphql
from .apis.grpc_api.server import start_grpc
from .apis.rest.app import start_rest
from .apis.soap.app import start_soap
from .apis.webhooks.app import start_webhooks
from .apis.websocket.server import start_websocket
from .context import ApiStarter, AppContext, RunningApi, create_context
from .core.config import Settings, load_settings
from .core.logging import configure_logging, get_logger

STARTERS: dict[str, ApiStarter] = {
    "rest": start_rest,
    "graphql": start_graphql,
    "grpc": start_grpc,
    "websocket": start_websocket,
    "webhooks": start_webhooks,
    "soap": start_soap,
}


async def start_all(ctx: AppContext, port_override: int | None = None) -> list[RunningApi]:
    unknown = set(ctx.settings.enabled_apis) - STARTERS.keys()
    if unknown:
        raise ValueError(f"Unknown APIs in ENABLED_APIS: {sorted(unknown)}")
    return [await STARTERS[name](ctx, port_override) for name in ctx.settings.enabled_apis]


async def serve(settings: Settings) -> None:
    configure_logging(settings.log_level, pretty=settings.env == "development")
    log = get_logger("main")
    ctx = create_context(settings)
    running = await start_all(ctx)
    for api in running:
        log.info("api_started", api=api.name, port=api.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting_down")
    # Reverse start order; each API stops accepting work, drains in-flight requests, then closes.
    await asyncio.gather(*(api.close() for api in reversed(running)), return_exceptions=True)
    log.info("shutdown_complete")


def run() -> None:
    asyncio.run(serve(load_settings()))


if __name__ == "__main__":
    run()
