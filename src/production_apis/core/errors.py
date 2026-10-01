"""One error taxonomy shared by every API.

Each protocol adapter maps ``ErrorCode`` to its native form (HTTP status, gRPC status,
GraphQL extension, SOAP fault) so business logic never knows which wire format it serves.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION = "VALIDATION"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"


class AppError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.PRECONDITION_FAILED: 412,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL: 500,
}


def to_app_error(exc: BaseException) -> AppError:
    return exc if isinstance(exc, AppError) else AppError(ErrorCode.INTERNAL, "Internal server error")
