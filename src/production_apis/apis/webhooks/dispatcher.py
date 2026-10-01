"""Outbound webhook delivery: at-least-once, signed, retried with exponential backoff + jitter.

In production the queue below is a durable transactional outbox (DB table / SQS / Kafka) so
deliveries survive restarts; the retry/backoff/dead-letter semantics are identical.
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from ...context import AppContext
from ...core.errors import AppError
from ...core.logging import get_logger
from ...domain.events import OrderEvent
from .signing import sign_headers
from .ssrf import assert_public_url

log = get_logger("webhooks")
EVENT_TYPES = frozenset({"order.created", "order.status_changed"})
RETRYABLE_4XX = {408, 425, 429}


@dataclass
class Endpoint:
    id: str
    url: str
    events: frozenset[str]
    secret: str
    active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class Delivery:
    id: str
    endpoint_id: str
    event_id: str
    event_type: str
    status: str = "pending"  # pending | delivered | failed (permanent) | dead (retries exhausted)
    attempts: int = 0
    last_status_code: int | None = None
    last_error: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class WebhookDispatcher:
    def __init__(self, ctx: AppContext, client: httpx.AsyncClient | None = None) -> None:
        self.ctx = ctx
        self.endpoints: dict[str, Endpoint] = {}
        self.deliveries: dict[str, Delivery] = {}
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(ctx.settings.webhook_timeout_seconds),
            follow_redirects=False,  # a redirect could bounce us to an internal address (SSRF)
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._off = ctx.orders.events.subscribe(self._on_event)

    # ---- registry ----
    async def register(self, url: str, events: list[str]) -> Endpoint:
        unknown = set(events) - EVENT_TYPES
        if unknown or not events:
            raise _validation(f"events must be a non-empty subset of {sorted(EVENT_TYPES)}")
        await assert_public_url(url, allow_private=self.ctx.settings.private_webhook_targets_allowed)
        endpoint = Endpoint(str(uuid.uuid4()), url, frozenset(events), "whsec_" + secrets.token_urlsafe(32))
        self.endpoints[endpoint.id] = endpoint
        return endpoint

    def remove(self, endpoint_id: str) -> bool:
        return self.endpoints.pop(endpoint_id, None) is not None

    # ---- delivery ----
    def _on_event(self, event: OrderEvent) -> None:
        for ep in list(self.endpoints.values()):
            if ep.active and event.type in ep.events:
                d = Delivery(str(uuid.uuid4()), ep.id, event.id, event.type)
                self.deliveries[d.id] = d
                task = asyncio.get_running_loop().create_task(self._deliver(ep, event, d))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def _backoff(self, attempt: int) -> float:
        base = self.ctx.settings.webhook_backoff_base_seconds
        return base * (2 ** (attempt - 1)) * random.uniform(0.5, 1.5)  # noqa: S311 - jitter, not security

    async def _deliver(self, ep: Endpoint, event: OrderEvent, d: Delivery) -> None:
        s = self.ctx.settings
        body = json.dumps(
            {
                "id": event.id,
                "type": event.type,
                "occurredAt": event.occurred_at,
                "data": event.data.model_dump(mode="json", by_alias=True),
            },
            separators=(",", ":"),
        ).encode()
        while d.attempts < s.webhook_max_attempts:
            d.attempts += 1
            outcome = await self._attempt(ep, event, body, d)
            d.updated_at = datetime.now(UTC).isoformat()
            self.ctx.metrics.webhook_deliveries.labels(outcome).inc()
            if outcome == "delivered":
                d.status = "delivered"
                return
            if outcome == "rejected":  # permanent client error: retrying cannot help
                d.status = "failed"
                return
            if d.attempts < s.webhook_max_attempts:
                await asyncio.sleep(self._backoff(d.attempts))
        d.status = "dead"  # exhausted -> dead-letter list for operator replay
        log.warning("webhook_dead_lettered", delivery=d.id, endpoint=ep.id, attempts=d.attempts)

    async def _attempt(self, ep: Endpoint, event: OrderEvent, body: bytes, d: Delivery) -> str:
        try:
            await assert_public_url(ep.url, allow_private=self.ctx.settings.private_webhook_targets_allowed)
            headers = {
                "content-type": "application/json",
                "user-agent": "northwind-webhooks/1.0",
                # Same id on every retry (receiver de-duplicates); timestamp + signature are fresh per attempt.
                **sign_headers(ep.secret, event.id, body, now=time.time()),
            }
            resp = await self._client.post(ep.url, content=body, headers=headers)
        except AppError as err:
            d.last_error = err.message
            return "rejected"
        except httpx.HTTPError as exc:
            d.last_error = type(exc).__name__
            d.last_status_code = None
            return "retry"
        d.last_status_code, d.last_error = resp.status_code, None
        if 200 <= resp.status_code < 300:
            return "delivered"
        if 400 <= resp.status_code < 500 and resp.status_code not in RETRYABLE_4XX:
            return "rejected"
        return "retry"

    async def aclose(self) -> None:
        self._off()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()

    async def drain(self, timeout: float = 5) -> None:
        """Wait for in-flight deliveries (used by graceful shutdown and tests)."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    def dead_letters(self) -> list[Delivery]:
        return [d for d in self.deliveries.values() if d.status == "dead"]


def _validation(msg: str) -> AppError:
    from ...core.errors import ErrorCode

    return AppError(ErrorCode.VALIDATION, msg)


def endpoint_view(ep: Endpoint, *, reveal_secret: bool = False) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": ep.id,
        "url": ep.url,
        "events": sorted(ep.events),
        "active": ep.active,
        "createdAt": ep.created_at,
    }
    if reveal_secret:
        view["secret"] = ep.secret  # shown exactly once, at creation
    return view
