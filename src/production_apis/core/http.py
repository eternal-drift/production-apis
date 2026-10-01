"""Cross-cutting HTTP middleware reused by the REST, GraphQL, SOAP and webhook-management apps."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .errors import HTTP_STATUS, AppError, ErrorCode

EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "cache-control": "no-store",
}


def problem(err: AppError, *, instance: str, request_id: str) -> JSONResponse:
    """RFC 9457 ``application/problem+json`` body for any AppError."""
    status = HTTP_STATUS[err.code]
    body: dict[str, object] = {
        "type": f"https://httpstatuses.io/{status}",
        "title": err.code.value,
        "status": status,
        "detail": err.message,
        "instance": instance,
        "requestId": request_id,
    }
    if err.details:
        body["errors"] = err.details
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request-ID propagation, security headers, body-size cap and per-client rate limiting."""

    def __init__(
        self,
        app: object,
        *,
        max_body_bytes: int = 100 * 1024,
        rate_limit: int = 120,
        window_seconds: float = 60,
        security_headers: bool = True,
        docs_prefixes: tuple[str, ...] = ("/docs", "/openapi.json"),
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.max_body_bytes = max_body_bytes
        self.rate_limit = rate_limit
        self.window = window_seconds
        self.security_headers = security_headers
        self.docs_prefixes = docs_prefixes
        self._hits: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

    def _check_rate(self, key: str) -> float | None:
        """Fixed-window limiter. Returns seconds until reset if limited. Swap for Redis when scaled out."""
        now = time.monotonic()
        start, count = self._hits[key]
        if now - start >= self.window:
            start, count = now, 0
        count += 1
        self._hits[key] = (start, count)
        if len(self._hits) > 10_000:  # bound memory against key-spraying
            self._hits = defaultdict(
                lambda: (0.0, 0), {k: v for k, v in self._hits.items() if now - v[0] < self.window}
            )
        return None if count <= self.rate_limit else max(0.0, self.window - (now - start))

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id", "")[:64] or str(uuid.uuid4())
        request.state.request_id = request_id

        response: Response
        if (
            request.url.path not in EXEMPT_PATHS
            and (retry := self._check_rate(request.client.host if request.client else "?")) is not None
        ):
            response = problem(
                AppError(ErrorCode.RATE_LIMITED, "Rate limit exceeded"),
                instance=request.url.path,
                request_id=request_id,
            )
            response.headers["retry-after"] = str(int(retry) + 1)
        elif int(request.headers.get("content-length") or 0) > self.max_body_bytes:
            response = problem(
                AppError(ErrorCode.VALIDATION, "Payload too large"),
                instance=request.url.path,
                request_id=request_id,
            )
            response.status_code = 413
        else:
            response = await call_next(request)

        response.headers["x-request-id"] = request_id
        if self.security_headers and not request.url.path.startswith(self.docs_prefixes):
            for k, v in SECURITY_HEADERS.items():
                response.headers.setdefault(k, v)
        return response
