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

    # The label the super-admin console is served on: `admin.<app_domain>`.
    # It is a reserved subdomain, so no clinic can ever claim it.
    admin_host_label: str = "admin"

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
    # Asking for a reset link is cheaper than signing in, and answering the
    # same either way, so it gets its own (tighter) budget.
    password_reset_rate_limit: str = "5/hour"  # noqa: S105 - a rate, not a secret

    # ------------------------------------------------------- invitations
    # An invitation is a one-time URL. The token is long-lived enough for a
    # clinic to act on it in their own time, a reset short enough that a link
    # sitting in an inbox is not a standing key.
    invite_token_ttl_seconds: int = 60 * 60 * 24 * 7
    reset_token_ttl_seconds: int = 60 * 60 * 2

    # ------------------------------------------------------------- email
    # Transactional mail — invitations and password resets. This is not the
    # patient-messaging outbox: that is per-clinic and provider-configured,
    # this is the platform speaking to a staff account. "console" writes the
    # link to the log, which is what development wants.
    mail_provider: Literal["console", "smtp"] = "console"
    mail_from: str = "Astrale PMS <no-reply@astrale.health>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_timeout_seconds: int = 15

    # ----------------------------------------------------------------- outbox
    # Where queued patient messages wait for the worker. The database row is
    # the source of truth; Redis only carries the wake-up, so losing Redis
    # delays delivery but never loses a message.
    redis_url: str = "redis://localhost:6380/0"
    outbox_stream: str = "pms:outbox"
    # A send is retried this many times before the message is parked as failed.
    message_max_attempts: int = 5
    message_retry_base_seconds: int = 30
    # How often the worker sweeps the table for due and stranded messages.
    worker_poll_seconds: int = 10
    # Appointment reminders go out this many hours before the slot.
    reminder_lead_hours: int = 24
    # Encrypts provider credentials at rest. Defaults to a key derived from
    # JWT_SECRET so development works out of the box; set your own to rotate
    # without invalidating sessions.
    secrets_key: str = ""

    # ---------------------------------------------------------------- support
    # Where a clinic's "enable this module" / "show me a walkthrough" request
    # is emailed. The request is stored first; this is only how the desk hears.
    support_email: str = "support@astrale.health"
    support_request_max_attempts: int = 5
    support_request_retry_base_seconds: int = 60
    # One user asking for the same thing again inside this window gets the
    # request they already made back, not a second email.
    support_request_dedupe_minutes: int = 10
    support_request_rate_limit: str = "20/hour"

    # ----------------------------------------------------------------- celery
    # Background jobs (support emails today). Defaults to the same Redis as the
    # outbox nudge. Late acks and reject-on-worker-lost mean a job killed by a
    # restart or deploy goes back on the queue instead of vanishing, and the
    # beat sweep re-queues anything the broker lost.
    celery_broker_url: str = ""
    celery_queue: str = "pms"
    # Must outlast the longest job, or Redis redelivers one that is still running.
    celery_visibility_timeout_seconds: int = 60 * 60
    celery_sweep_seconds: int = 60

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @field_validator("app_domain")
    @classmethod
    def _strip_domain(cls, v: str) -> str:
        return v.strip().lower().lstrip(".")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def admin_host(self) -> str:
        """Where the super-admin console lives — `admin.<app_domain>`."""
        return f"{self.admin_host_label}.{self.app_domain}"

    def public_origin(self, host: str) -> str:
        """An absolute origin for a host this deployment serves.

        Links in email are built from configuration, never from the `Host` of
        the request that triggered them — a forged header must not be able to
        decide where an invitation points.
        """
        port = f":{self.public_port}" if self.public_port else ""
        return f"{self.public_scheme}://{host}{port}"

    def tenant_origin(self, slug: str) -> str:
        return self.public_origin(f"{slug}.{self.app_domain}")

    @property
    def admin_origin(self) -> str:
        return self.public_origin(self.admin_host)

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
        if self.mail_provider == "console":
            # Invitations and resets would be written to the log instead of
            # being delivered, and nobody would be able to sign in.
            problems.append("MAIL_PROVIDER must be a real transport, not 'console'")
        if self.public_scheme != "https":
            problems.append("PUBLIC_SCHEME must be https")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
