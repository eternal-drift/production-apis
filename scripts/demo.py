"""Run journey J1 end to end: `uv run python scripts/demo.py`."""

import asyncio

from production_apis.context import create_context
from production_apis.core.config import load_settings
from production_apis.core.logging import configure_logging
from production_apis.journey import run_journey


async def main() -> None:
    configure_logging(quiet=True)
    ctx = create_context(load_settings(APP_ENV="test", host="127.0.0.1", webhook_backoff_base_seconds=0.05))
    print("Journey J1: Sam buys a tent — one order, six consumers\n")
    await run_journey(ctx, print)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
