"""Twelve-factor configuration: validated once at startup, fails fast with readable errors."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DEV_SECRET = "dev-only-secret-do-not-use-in-production-0123456789"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = Field("development", alias="APP_ENV")
    log_level: str = "INFO"
    host: str = "0.0.0.0"  # noqa: S104 - containers must bind all interfaces
    enabled_apis: Annotated[list[str], NoDecode] = [
        "rest",
        "graphql",
        "grpc",
        "websocket",
        "webhooks",
        "soap",
    ]

    rest_port: int = 3001
    graphql_port: int = 3002
    grpc_port: int = 3003
    websocket_port: int = 3004
    webhooks_port: int = 3005
    soap_port: int = 3006
    webhook_demo_receiver_port: int = 3007

    jwt_secret: str | None = Field(None, min_length=32)
    jwt_issuer: str = "production-apis"
    jwt_audience: str = "production-apis-clients"

    rate_limit_max: int = Field(120, gt=0)
    rate_limit_window_seconds: float = Field(60, gt=0)
    cors_allowed_origins: Annotated[list[str], NoDecode] = []

    graphql_max_depth: int = 8
    graphql_introspection: bool | None = None

    ws_max_connections: int = 1000
    ws_max_message_bytes: int = 16 * 1024
    ws_heartbeat_seconds: float = 30
    ws_allowed_origins: Annotated[list[str], NoDecode] = []

    webhook_max_attempts: int = 6
    webhook_timeout_seconds: float = 5
    webhook_backoff_base_seconds: float = 1
    webhook_allow_private_targets: bool | None = None
    webhook_demo_secret: str = Field("whsec_demo_secret_change_me", min_length=16)

    @field_validator("enabled_apis", "cors_allowed_origins", "ws_allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "production" and not self.jwt_secret:
            raise ValueError("JWT_SECRET is required in production")
        return self

    # Secure by default: only non-production environments get permissive fallbacks.
    @property
    def secret(self) -> str:
        return self.jwt_secret or _DEV_SECRET

    @property
    def is_prod(self) -> bool:
        return self.env == "production"

    @property
    def introspection_enabled(self) -> bool:
        return self.graphql_introspection if self.graphql_introspection is not None else not self.is_prod

    @property
    def private_webhook_targets_allowed(self) -> bool:
        if self.webhook_allow_private_targets is not None:
            return self.webhook_allow_private_targets
        return not self.is_prod


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
