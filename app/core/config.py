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

    # --------------------------------------------------- platform billing
    # What a clinic pays *us*. Nothing here is a price: every amount the
    # product charges lives in the `plans`, `plan_features` and `tax_rates`
    # tables, and a deployment that wants different prices writes rows, not
    # settings. These are the knobs around the money, not the money.
    billing_enabled: bool = True
    #: The gateway new subscriptions are created against. Adding Cashfree or
    #: Stripe is a new adapter package plus a value here — the billing engine
    #: never reads it.
    billing_gateway: Literal["razorpay", "manual"] = "razorpay"
    #: Printed on the invoice, in front of the platform-wide sequence.
    billing_invoice_prefix: str = "AST"
    #: How long a clinic has to pay before an invoice is chased.
    billing_payment_terms_days: int = 7
    #: How long past due before the subscription moves to `past_due`. Suspension
    #: is a separate, deliberate act by the platform, never an automatic one —
    #: cutting a clinic off from its own patient records is not a billing
    #: decision.
    billing_grace_days: int = 7
    #: The state or region the platform itself bills from. Only consulted when
    #: the `tax_rates` table draws an intra-state/inter-state distinction; a
    #: deployment whose rates are all `any` never reads it.
    billing_supplier_region: str = ""
    #: Where the "Pay Now" button in the invoice email points when the gateway
    #: has not minted a hosted page. Always a path on the clinic's own origin.
    billing_portal_path: str = "/settings/billing"
    #: How often the billing worker sweeps for due cycles, unsent emails and
    #: unprocessed webhooks.
    billing_poll_seconds: int = 60
    #: A webhook that keeps failing is parked after this many attempts and
    #: waits for someone to look at it.
    webhook_max_attempts: int = 8
    webhook_retry_base_seconds: int = 30

    # ------------------------------------------------------------- Razorpay
    # The secret and the webhook secret never leave this process. `key_id` is
    # the only value that may reach a browser — it is the public half of the
    # pair, and checkout needs it. There is no setting here that exposes
    # anything else, on purpose.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    #: Razorpay signs each delivery with this. A deployment with webhooks
    #: configured and no secret set cannot verify anything, so the handler
    #: refuses every delivery rather than trusting it.
    razorpay_webhook_secret: str = ""
    razorpay_api_base: str = "https://api.razorpay.com/v1"
    razorpay_timeout_seconds: int = 20

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

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
        if self.billing_enabled and self.billing_gateway == "razorpay":
            # A production deployment that bills through Razorpay but cannot
            # authenticate to it would take orders it can never collect, and —
            # worse — would accept unverifiable webhooks.
            if not self.razorpay_configured:
                problems.append("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set")
            if not self.razorpay_webhook_secret:
                problems.append("RAZORPAY_WEBHOOK_SECRET must be set")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
