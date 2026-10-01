"""Mint a development JWT: `uv run python scripts/mint_token.py [subject] [scope ...]`."""

import sys

from production_apis.core.auth import Authenticator
from production_apis.core.config import load_settings

subject = sys.argv[1] if len(sys.argv) > 1 else "dev-user"
scopes = sys.argv[2:] or ["orders:read", "orders:write"]
settings = load_settings()
if settings.is_prod:
    sys.exit("Refusing to mint tokens in production; use your identity provider.")
print(Authenticator(settings).sign(subject, scopes))
