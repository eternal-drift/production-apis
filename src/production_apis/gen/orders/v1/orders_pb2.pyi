from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class OrderStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ORDER_STATUS_UNSPECIFIED: _ClassVar[OrderStatus]
    ORDER_STATUS_PENDING: _ClassVar[OrderStatus]
    ORDER_STATUS_PAID: _ClassVar[OrderStatus]
    ORDER_STATUS_SHIPPED: _ClassVar[OrderStatus]
    ORDER_STATUS_CANCELLED: _ClassVar[OrderStatus]
ORDER_STATUS_UNSPECIFIED: OrderStatus
ORDER_STATUS_PENDING: OrderStatus
ORDER_STATUS_PAID: OrderStatus
ORDER_STATUS_SHIPPED: OrderStatus
ORDER_STATUS_CANCELLED: OrderStatus

class OrderItem(_message.Message):
    __slots__ = ("sku", "quantity", "unit_price_cents")
    SKU_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_FIELD_NUMBER: _ClassVar[int]
    UNIT_PRICE_CENTS_FIELD_NUMBER: _ClassVar[int]
    sku: str
    quantity: int
    unit_price_cents: int
    def __init__(self, sku: _Optional[str] = ..., quantity: _Optional[int] = ..., unit_price_cents: _Optional[int] = ...) -> None: ...

class Order(_message.Message):
    __slots__ = ("id", "customer_id", "currency", "items", "total_cents", "status", "version", "created_at", "updated_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    CUSTOMER_ID_FIELD_NUMBER: _ClassVar[int]
    CURRENCY_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_CENTS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    customer_id: str
    currency: str
    items: _containers.RepeatedCompositeFieldContainer[OrderItem]
    total_cents: int
    status: OrderStatus
    version: int
    created_at: str
    updated_at: str
    def __init__(self, id: _Optional[str] = ..., customer_id: _Optional[str] = ..., currency: _Optional[str] = ..., items: _Optional[_Iterable[_Union[OrderItem, _Mapping]]] = ..., total_cents: _Optional[int] = ..., status: _Optional[_Union[OrderStatus, str]] = ..., version: _Optional[int] = ..., created_at: _Optional[str] = ..., updated_at: _Optional[str] = ...) -> None: ...

class CreateOrderRequest(_message.Message):
    __slots__ = ("customer_id", "currency", "items", "idempotency_key")
    CUSTOMER_ID_FIELD_NUMBER: _ClassVar[int]
    CURRENCY_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    customer_id: str
    currency: str
    items: _containers.RepeatedCompositeFieldContainer[OrderItem]
    idempotency_key: str
    def __init__(self, customer_id: _Optional[str] = ..., currency: _Optional[str] = ..., items: _Optional[_Iterable[_Union[OrderItem, _Mapping]]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class GetOrderRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ListOrdersRequest(_message.Message):
    __slots__ = ("customer_id", "status", "page_size", "page_token")
    CUSTOMER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    customer_id: str
    status: OrderStatus
    page_size: int
    page_token: str
    def __init__(self, customer_id: _Optional[str] = ..., status: _Optional[_Union[OrderStatus, str]] = ..., page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListOrdersResponse(_message.Message):
    __slots__ = ("orders", "next_page_token")
    ORDERS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    orders: _containers.RepeatedCompositeFieldContainer[Order]
    next_page_token: str
    def __init__(self, orders: _Optional[_Iterable[_Union[Order, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class UpdateOrderStatusRequest(_message.Message):
    __slots__ = ("id", "status", "expected_version")
    ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    id: str
    status: OrderStatus
    expected_version: int
    def __init__(self, id: _Optional[str] = ..., status: _Optional[_Union[OrderStatus, str]] = ..., expected_version: _Optional[int] = ...) -> None: ...

class WatchOrdersRequest(_message.Message):
    __slots__ = ("customer_id",)
    CUSTOMER_ID_FIELD_NUMBER: _ClassVar[int]
    customer_id: str
    def __init__(self, customer_id: _Optional[str] = ...) -> None: ...

class OrderEvent(_message.Message):
    __slots__ = ("id", "type", "occurred_at", "order")
    ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_FIELD_NUMBER: _ClassVar[int]
    ORDER_FIELD_NUMBER: _ClassVar[int]
    id: str
    type: str
    occurred_at: str
    order: Order
    def __init__(self, id: _Optional[str] = ..., type: _Optional[str] = ..., occurred_at: _Optional[str] = ..., order: _Optional[_Union[Order, _Mapping]] = ...) -> None: ...
