"""Settings. Every value comes from the environment; nothing security-relevant
has a usable default outside development."""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Environment = "development"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://pms_app:app@localhost:5432/pms"
    migration_database_url: str = "postgresql+asyncpg://pms_owner:owner@localhost:5432/pms"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    app_domain: str = "localhost"
    extra_cors_origins: str = ""
    public_scheme: str = "http"
    public_port: str = "3000"

    jwt_secret: str = "development-only-secret"  # noqa: S105 - refused in production, see below
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    max_failed_logins: int = 5
    cookie_secure: bool = False
    cookie_domain: str = ""
    # Where the refresh cookie is sent back to. It must match the path the
    # *browser* uses, which is not necessarily the path this app sees: served
    # behind a proxy at `/api`, the browser's refresh URL is `/api/v1/auth/
    # refresh`. Narrow this to that prefix when it is fixed; "/" always works.
    refresh_cookie_path: str = "/"

    # Development escape hatch: let `X-Tenant-Slug` stand in for the subdomain
    # when the API is reached through a proxy that rewrites Host. Never in
    # production — the header is refused there whatever this says.
    allow_tenant_header: bool = False

    login_rate_limit: str = "10/minute"
    api_rate_limit: str = "600/minute"

    @field_validator("app_domain")
    @classmethod
    def _strip_domain(cls, v: str) -> str:
        return v.strip().lower().lstrip(".")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origins(self) -> list[str]:
        """Explicit extra origins. Tenant subdomains are matched by regex instead,
        because they are created at runtime and cannot be enumerated up front."""
        return [o.strip() for o in self.extra_cors_origins.split(",") if o.strip()]

    @property
    def cors_origin_regex(self) -> str:
        """`https://anything.apex[:port]` plus the apex itself."""
        domain = self.app_domain.replace(".", r"\.")
        return rf"^https?://([a-z0-9][a-z0-9-]*\.)*{domain}(:\d+)?$"

    def validate_for_production(self) -> list[str]:
        """Refuse to boot a production process on development defaults."""
        problems: list[str] = []
        if not self.is_production:
            return problems
        defaults = {
            "development-only-secret",
            "change-me-in-production-this-value-is-not-a-secret",
        }
        if self.jwt_secret in defaults:
            problems.append("JWT_SECRET is still the development default")
        if len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be at least 32 characters")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production")
        if self.debug:
            problems.append("DEBUG must be false in production")
        if self.allow_tenant_header:
            problems.append("ALLOW_TENANT_HEADER must be false in production")
        if "pms_owner" in self.database_url:
            problems.append("DATABASE_URL must use the unprivileged app role, not the table owner")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
