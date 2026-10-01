"""Prometheus metrics shared by every API."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

from .errors import AppError, to_app_error

T = TypeVar("T")


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        for collector in (GC_COLLECTOR, PLATFORM_COLLECTOR, PROCESS_COLLECTOR):
            with contextlib.suppress(ValueError):  # already registered
                self.registry.register(collector)
        self.requests = Counter(
            "api_requests_total",
            "Operations handled",
            ["api", "operation", "outcome"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "api_request_duration_seconds",
            "Operation latency",
            ["api", "operation"],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
            registry=self.registry,
        )
        self.connections = Gauge(
            "api_active_connections", "Open long-lived connections", ["api"], registry=self.registry
        )
        self.webhook_deliveries = Counter(
            "webhook_deliveries_total", "Webhook delivery attempts", ["outcome"], registry=self.registry
        )

    async def observe(self, api: str, operation: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Time an operation and record its outcome, whichever API it belongs to."""
        start = time.perf_counter()
        try:
            result = await fn()
        except Exception as exc:
            err: AppError = to_app_error(exc)
            self.requests.labels(api, operation, err.code.value.lower()).inc()
            raise
        else:
            self.requests.labels(api, operation, "ok").inc()
            return result
        finally:
            self.duration.labels(api, operation).observe(time.perf_counter() - start)
