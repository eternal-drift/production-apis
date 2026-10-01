from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..core.errors import AppError, ErrorCode


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"


class OrderItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    sku: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    quantity: int = Field(ge=1, le=1000)
    unit_price_cents: int = Field(ge=0, le=100_000_000, alias="unitPriceCents")


class CreateOrder(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    customer_id: Annotated[str, StringConstraints(min_length=1, max_length=64)] = Field(alias="customerId")
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "USD"  # ISO 4217
    items: Annotated[list[OrderItem], Field(min_length=1, max_length=100)]


class Order(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)
    id: UUID
    customer_id: str = Field(alias="customerId")
    currency: str
    items: list[OrderItem]
    total_cents: int = Field(alias="totalCents")
    status: OrderStatus
    version: int  # optimistic-concurrency token, incremented on every change
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class OrderPage(BaseModel):
    items: list[Order]
    next_cursor: str | None = Field(None, alias="nextCursor")
    model_config = ConfigDict(populate_by_name=True)


# Legal state transitions. Terminal states have no outgoing edges.
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.PAID, OrderStatus.CANCELLED}),
    OrderStatus.PAID: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.SHIPPED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


def assert_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if dst not in TRANSITIONS[src]:
        raise AppError(ErrorCode.CONFLICT, f"Illegal status transition {src} -> {dst}")


def validate_create(data: object) -> CreateOrder:
    """Validate untrusted input; surface failures as VALIDATION errors with field paths."""
    from pydantic import ValidationError

    try:
        return CreateOrder.model_validate(data)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.VALIDATION,
            "Request validation failed",
            [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()],
        ) from exc
