"""JWT bearer authentication with scope-based authorization, shared by all APIs."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import jwt

from .config import Settings
from .errors import AppError, ErrorCode

SCOPE_READ = "orders:read"
SCOPE_WRITE = "orders:write"


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AppError(ErrorCode.FORBIDDEN, f"Missing required scope: {scope}")


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.secret
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience

    def verify(self, token: str | None) -> Principal:
        if not token:
            raise AppError(ErrorCode.UNAUTHENTICATED, "Missing bearer token")
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],  # pinned: no "alg: none" / algorithm-confusion downgrade
                issuer=self._issuer,
                audience=self._audience,
                leeway=5,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            # Deliberately vague: do not leak why verification failed.
            raise AppError(ErrorCode.UNAUTHENTICATED, "Invalid or expired token") from exc
        return Principal(claims["sub"], frozenset(str(claims.get("scope", "")).split()))

    def sign(
        self, subject: str, scopes: Iterable[str] = (SCOPE_READ, SCOPE_WRITE), ttl_seconds: int = 3600
    ) -> str:
        """Mint a token (scripts/tests only; real deployments use an identity provider)."""
        now = int(time.time())
        return jwt.encode(
            {
                "sub": subject,
                "scope": " ".join(scopes),
                "iss": self._issuer,
                "aud": self._audience,
                "iat": now,
                "exp": now + ttl_seconds,
            },
            self._secret,
            algorithm="HS256",
        )


_BEARER = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)


def bearer_from(header: str | None) -> str | None:
    match = _BEARER.match(header or "")
    return match.group(1) if match else None
