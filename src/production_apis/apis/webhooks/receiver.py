"""Reference webhook *consumer*: what a partner (accounting, carrier, Slack bot) implements.

Demonstrates the three rules every receiver must follow:
  1. verify the signature over the RAW body before trusting anything,
  2. be idempotent (deliveries are at-least-once; the same webhook-id may arrive twice),
  3. acknowledge fast with 2xx and do heavy work asynchronously.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...context import RunningApi, serve_asgi
from .signing import SignatureError, verify


def create_receiver_app(secret: str | list[str], *, fail_first: int = 0) -> FastAPI:
    """``fail_first`` makes the first N requests return 503, simulating a partner outage (S5.4)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.secret = secret  # mutable so a receiver can be created before its secret is issued
    app.state.processed = {}  # webhook-id -> payload
    app.state.duplicates = 0
    app.state.rejected = 0
    app.state.requests = 0
    remaining_failures = fail_first

    @app.post("/hooks/orders")
    async def receive(request: Request) -> JSONResponse:
        nonlocal remaining_failures
        app.state.requests += 1
        body = await request.body()  # raw bytes: re-serialising parsed JSON would break the signature
        try:
            webhook_id = verify(app.state.secret, dict(request.headers), body)
        except SignatureError:
            app.state.rejected += 1
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        if remaining_failures > 0:
            remaining_failures -= 1
            return JSONResponse({"error": "temporarily unavailable"}, status_code=503)
        if webhook_id in app.state.processed:
            app.state.duplicates += 1  # already handled: acknowledge without reprocessing
            return JSONResponse({"status": "duplicate"})
        app.state.processed[webhook_id] = json.loads(body)
        return JSONResponse({"status": "processed"})

    @app.get("/received")
    async def received() -> dict[str, Any]:
        return {"processed": list(app.state.processed.values()), "duplicates": app.state.duplicates}

    return app


async def start_receiver(host: str, port: int, secret: str) -> tuple[RunningApi, FastAPI]:
    app = create_receiver_app(secret)
    return await serve_asgi("webhook-demo-receiver", app, host, port), app
