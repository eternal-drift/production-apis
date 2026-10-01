from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import Order


@dataclass(frozen=True)
class OrderEvent:
    type: str  # order.created | order.status_changed
    data: Order
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class EventBus:
    """In-process pub/sub fanning domain events out to streaming APIs and the webhook dispatcher.

    Multi-instance deployments replace this seam with Redis / NATS / Kafka.
    """

    def __init__(self) -> None:
        self._subscribers: set[Callable[[OrderEvent], None]] = set()

    def publish(self, type_: str, order: Order) -> OrderEvent:
        event = OrderEvent(type_, order)
        for sub in list(self._subscribers):
            with contextlib.suppress(Exception):  # isolate subscribers from each other
                sub(event)
        return event

    def subscribe(self, listener: Callable[[OrderEvent], None]) -> Callable[[], None]:
        self._subscribers.add(listener)
        return lambda: self._subscribers.discard(listener)

    async def stream(
        self, predicate: Callable[[OrderEvent], bool] = lambda _e: True, *, max_buffer: int = 1000
    ) -> AsyncIterator[OrderEvent]:
        """Async iterator view. Bounded: slow consumers lose events instead of exhausting memory."""
        queue: asyncio.Queue[OrderEvent] = asyncio.Queue(maxsize=max_buffer)

        def push(event: OrderEvent) -> None:
            if predicate(event):
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

        off = self.subscribe(push)
        try:
            while True:
                yield await queue.get()
        finally:
            off()
