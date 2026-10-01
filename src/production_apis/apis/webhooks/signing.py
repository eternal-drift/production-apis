"""Webhook signing and verification (Standard-Webhooks / Stripe style).

signature = HMAC-SHA256(secret, f"{webhook_id}.{timestamp}.{raw_body}")

The id and timestamp are inside the signed payload, so neither can be altered or replayed
with a different message, and old captures are rejected by the timestamp tolerance.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

TOLERANCE_SECONDS = 300
HEADER_ID, HEADER_TS, HEADER_SIG = "webhook-id", "webhook-timestamp", "webhook-signature"


class SignatureError(Exception):
    pass


def compute_signature(secret: str, webhook_id: str, timestamp: int | str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{webhook_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return f"v1={mac.hexdigest()}"


def sign_headers(secret: str, webhook_id: str, body: bytes, *, now: float | None = None) -> dict[str, str]:
    ts = int(now if now is not None else time.time())
    return {
        HEADER_ID: webhook_id,
        HEADER_TS: str(ts),
        HEADER_SIG: compute_signature(secret, webhook_id, ts, body),
    }


def verify(
    secrets: str | list[str],
    headers: Mapping[str, str],
    body: bytes,
    *,
    tolerance: int = TOLERANCE_SECONDS,
    now: float | None = None,
) -> str:
    """Verify a delivery and return its webhook id. Raises SignatureError.

    ``secrets`` may hold several secrets (current + previous) to support zero-downtime rotation;
    the header may carry several space-separated signatures for the same reason.
    """
    h = {k.lower(): v for k, v in headers.items()}
    try:
        webhook_id, ts_raw, sig_header = h[HEADER_ID], h[HEADER_TS], h[HEADER_SIG]
        ts = int(ts_raw)
    except (KeyError, ValueError) as exc:
        raise SignatureError("missing or malformed webhook headers") from exc

    if abs((now if now is not None else time.time()) - ts) > tolerance:
        raise SignatureError("timestamp outside tolerance (possible replay)")

    candidates = [secrets] if isinstance(secrets, str) else secrets
    provided = sig_header.split()
    for secret in candidates:
        expected = compute_signature(secret, webhook_id, ts, body)
        # Constant-time comparison: never compare signatures with ==.
        if any(hmac.compare_digest(expected, p) for p in provided):
            return webhook_id
    raise SignatureError("signature mismatch")
