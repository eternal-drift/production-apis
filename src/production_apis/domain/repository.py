from __future__ import annotations

import asyncio
import builtins
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..core.errors import AppError, ErrorCode
from .models import Order, OrderStatus


@dataclass(frozen=True)
class Row:
    order: Order
    seq: int


class OrderRepository(Protocol):
    async def insert(self, order: Order) -> int: ...
    async def get(self, order_id: UUID) -> Order | None: ...
    async def update(self, order: Order, expected_version: int) -> None:
        """Compare-and-swap on ``version``; raises PRECONDITION_FAILED on mismatch."""
        ...

    async def list(
        self, *, customer_id: str | None, status: OrderStatus | None, after_seq: int, limit: int
    ) -> tuple[builtins.list[Row], bool]: ...

    async def by_customers(self, ids: builtins.list[str]) -> dict[str, builtins.list[Order]]: ...
    async def ping(self) -> None: ...


class InMemoryOrderRepository:
    """Reference implementation. Swap for Postgres by implementing the same Protocol."""

    def __init__(self) -> None:
        self._rows: dict[UUID, Row] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    async def insert(self, order: Order) -> int:
        async with self._lock:
            self._seq += 1
            self._rows[order.id] = Row(order, self._seq)
            return self._seq

    async def get(self, order_id: UUID) -> Order | None:
        row = self._rows.get(order_id)
        return row.order if row else None

    async def update(self, order: Order, expected_version: int) -> None:
        async with self._lock:
            row = self._rows.get(order.id)
            if row is None:
                raise AppError(ErrorCode.NOT_FOUND, f"Order {order.id} not found")
            if row.order.version != expected_version:
                raise AppError(
                    ErrorCode.PRECONDITION_FAILED,
                    "Order was modified by another request",
                    {"currentVersion": row.order.version},
                )
            self._rows[order.id] = Row(order, row.seq)

    async def list(
        self, *, customer_id: str | None, status: OrderStatus | None, after_seq: int, limit: int
    ) -> tuple[builtins.list[Row], bool]:
        matches = sorted(
            (
                r
                for r in self._rows.values()
                if r.seq > after_seq
                and (customer_id is None or r.order.customer_id == customer_id)
                and (status is None or r.order.status == status)
            ),
            key=lambda r: r.seq,
        )
        return matches[:limit], len(matches) > limit

    async def by_customers(self, ids: builtins.list[str]) -> dict[str, builtins.list[Order]]:
        out: dict[str, builtins.list[Order]] = {i: [] for i in ids}
        for row in self._rows.values():
            if row.order.customer_id in out:
                out[row.order.customer_id].append(row.order)
        return out

    async def ping(self) -> None:
        return None
