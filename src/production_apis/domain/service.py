from __future__ import annotations

import base64
import builtins
import hashlib
import re
import uuid
from datetime import UTC, datetime
from uuid import UUID

from ..core.errors import AppError, ErrorCode
from .events import EventBus
from .models import Order, OrderPage, OrderStatus, assert_transition, validate_create
from .repository import OrderRepository

MAX_PAGE = 100


def _encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"s:{seq}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except Exception as exc:
        raise AppError(ErrorCode.VALIDATION, "Invalid cursor") from exc
    match = re.fullmatch(r"s:(\d+)", raw)
    if not match:
        raise AppError(ErrorCode.VALIDATION, "Invalid cursor")
    return int(match.group(1))


class OrderService:
    """Protocol-agnostic business logic. Every API adapter calls only this."""

    def __init__(self, repo: OrderRepository, events: EventBus) -> None:
        self._repo = repo
        self.events = events
        # idempotency key -> (request fingerprint, resulting order id)
        self._idempotency: dict[str, tuple[str, UUID]] = {}

    async def create(self, payload: object, idempotency_key: str | None = None) -> tuple[Order, bool]:
        """Returns ``(order, replayed)``."""
        data = validate_create(payload)
        fingerprint = hashlib.sha256(data.model_dump_json().encode()).hexdigest()

        if idempotency_key and (prior := self._idempotency.get(idempotency_key)):
            if prior[0] != fingerprint:
                raise AppError(
                    ErrorCode.CONFLICT, "Idempotency-Key was already used with a different request body"
                )
            if existing := await self._repo.get(prior[1]):
                return existing, True

        now = datetime.now(UTC)
        order = Order(
            id=uuid.uuid4(),
            customer_id=data.customer_id,
            currency=data.currency,
            items=data.items,
            total_cents=sum(i.quantity * i.unit_price_cents for i in data.items),
            status=OrderStatus.PENDING,
            version=1,
            created_at=now,
            updated_at=now,
        )
        await self._repo.insert(order)
        if idempotency_key:
            self._idempotency[idempotency_key] = (fingerprint, order.id)
        self.events.publish("order.created", order)
        return order, False

    async def get(self, order_id: UUID | str) -> Order:
        try:
            oid = order_id if isinstance(order_id, UUID) else UUID(order_id)
        except ValueError as exc:
            raise AppError(ErrorCode.VALIDATION, "Order id must be a UUID") from exc
        order = await self._repo.get(oid)
        if order is None:
            raise AppError(ErrorCode.NOT_FOUND, f"Order {oid} not found")
        return order

    async def list(
        self,
        *,
        customer_id: str | None = None,
        status: OrderStatus | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> OrderPage:
        if not 1 <= limit <= MAX_PAGE:
            raise AppError(ErrorCode.VALIDATION, f"limit must be between 1 and {MAX_PAGE}")
        rows, has_more = await self._repo.list(
            customer_id=customer_id,
            status=status,
            after_seq=_decode_cursor(cursor) if cursor else 0,
            limit=limit,
        )
        next_cursor = _encode_cursor(rows[-1].seq) if has_more and rows else None
        return OrderPage(items=[r.order for r in rows], nextCursor=next_cursor)

    async def transition(
        self, order_id: UUID | str, to: OrderStatus, expected_version: int | None = None
    ) -> Order:
        """``expected_version`` enables optimistic concurrency (If-Match in REST)."""
        current = await self.get(order_id)
        if expected_version is not None and expected_version != current.version:
            raise AppError(
                ErrorCode.PRECONDITION_FAILED, "Order version mismatch", {"currentVersion": current.version}
            )
        assert_transition(current.status, to)
        updated = current.model_copy(
            update={"status": to, "version": current.version + 1, "updated_at": datetime.now(UTC)}
        )
        await self._repo.update(updated, current.version)
        self.events.publish("order.status_changed", updated)
        return updated

    async def orders_by_customers(self, ids: builtins.list[str]) -> dict[str, builtins.list[Order]]:
        return await self._repo.by_customers(ids)

    async def ready(self) -> None:
        await self._repo.ping()
