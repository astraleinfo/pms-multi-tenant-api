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

    # The PMS's own clock. New clinics start on it, a clinic with no valid
    # timezone falls back to it, and the scheduled jobs keep it: "midnight" for
    # the reminder run is midnight here.
    pms_timezone: str = "Asia/Kolkata"

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
    # An invitation is a one-time URL. The token TTL (how long the link stays
    # valid) and reset token TTL are configured in `platform_settings` (see
    # app/services/platform.py::Timings) — editable from the console, not env vars.

    # ------------------------------------------------------------- email
    # Transactional mail — invitations and password resets. This is not the
    # patient-messaging outbox: that is per-clinic and provider-configured,
    # this is the platform speaking to a staff account. "console" writes the
    # link to the log, which is what development wants; "smtp" hands it to a
    # mail server; "ses" sends it through Amazon SES with the AWS settings below.
    mail_provider: Literal["console", "smtp", "ses"] = "console"
    mail_from: str = "Astrale PMS <no-reply@astrale.health>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_timeout_seconds: int = 15
    #: SES region, when it differs from AWS_REGION — SES is not in every region
    #: and a verified sending identity belongs to one.
    ses_region: str = ""
    #: An SES configuration set, for delivery, bounce and complaint events.
    ses_configuration_set: str = ""

    # ------------------------------------------------------------------- AWS
    # Shared by S3 and SES. Leave the keys empty to use boto3's own chain — an
    # instance or task role, `~/.aws`, or the standard environment variables —
    # which is what a deployment on AWS should do rather than hold a key here.
    aws_region: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    # --------------------------------------------------------------- storage
    # Where uploaded files live — today, the reports a lab order collects.
    #   database  the bytes sit in the row, under the same RLS as the order
    #   s3        the bytes go to a bucket; the row keeps the key and metadata
    # Reads follow the row, not this setting, so switching backend never
    # strands a file that was stored under the other one.
    storage_backend: Literal["database", "s3"] = "database"
    s3_bucket: str = ""
    #: defaults to AWS_REGION
    s3_region: str = ""
    #: for an S3-compatible store (MinIO, R2, ...). Empty means AWS itself.
    s3_endpoint_url: str = ""
    #: every key starts here, so one bucket can serve several deployments
    s3_prefix: str = "pms"
    #: "AES256", "aws:kms", or empty to leave it to the bucket's default
    s3_server_side_encryption: str = "AES256"
    s3_kms_key_id: str = ""

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

    # ------------------------------------------------------ subscription rent
    # What each clinic pays the platform for the modules it has switched on.
    # When it's due, how long the grace period is, and when the reminder
    # ladder starts are all in `platform_settings` now (see
    # app/services/platform.py::Timings) — editable from the console, not
    # env vars.
    #
    # Off by default: switched on, the beat raises real bills and emails real
    # admins — including every seeded demo clinic. The console's "Run billing
    # now" works either way, so the cycle can be tried by hand first.
    rent_billing_enabled: bool = False
    rent_currency: str = "INR"
    #: how often the beat runs the cycle; every run is idempotent
    rent_cycle_seconds: int = 3600

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

    # ------------------------------------------------- background-job monitor
    # Flower watches Celery's event stream and keeps the task history the
    # console's jobs section reads. It is never published: it answers on the
    # internal container network only, and the one way to it from outside is
    # `/v1/platform/jobs/...`, which is behind a super-admin check.
    #
    # Switching this off leaves the routes in place — they still refuse anyone
    # who is not a super admin — and reports the monitor as unavailable.
    flower_enabled: bool = True
    flower_url: str = "http://flower:5555"
    #: What Flower is told it is mounted at (`--url-prefix`). Flower builds its
    #: own links from it, so it has to be the path the *browser* uses, and the
    #: API has to forward to the same one. Behind a proxy that serves this API
    #: under `/api` — which is what the web app's rewrite does — set it to
    #: `api/v1/platform/jobs/flower` on both the API and the Flower container.
    flower_url_prefix: str = "v1/platform/jobs/flower"
    #: `user:password`, matching `--basic-auth` on the Flower container. A
    #: private network is not an authorisation story on its own: anything else
    #: that lands on it would otherwise have the whole job history.
    flower_basic_auth: str = ""
    flower_timeout_seconds: float = 10.0
    #: How long the cookie that lets a browser load Flower's own pages lasts.
    #: Short on purpose — the console mints a new one each time it opens the
    #: section, and the cookie is good for nothing else (see the `scp` claim).
    flower_session_ttl_seconds: int = 900
    #: Where that cookie is sent back to — the same trap as the refresh cookie:
    #: it must match the path the *browser* uses, not the one this app sees.
    flower_cookie_path: str = "/"
    #: How many task records to read from Flower for one screen. Flower holds a
    #: bounded history in memory; this is the window the console counts over,
    #: and it is reported alongside the totals so nobody reads them as all time.
    flower_task_window: int = 500
    #: How often to make Flower re-inspect the workers. Flower inspects once at
    #: startup and then never again unless a request asks it to — and in a
    #: container set the monitor usually starts before any worker exists, so
    #: that one inspection finds nothing and the concurrency and queue columns
    #: would stay empty for the life of the process. This is a ping to every
    #: worker, so it is throttled rather than done on each poll of the screen.
    flower_inspect_seconds: float = 30.0

    @property
    def flower_api_base(self) -> str:
        """Where this API talks to Flower, prefix included."""
        prefix = self.flower_url_prefix.strip("/")
        base = self.flower_url.rstrip("/")
        return f"{base}/{prefix}" if prefix else base

    @property
    def flower_credentials(self) -> tuple[str, str] | None:
        user, _, password = self.flower_basic_auth.partition(":")
        return (user, password) if user else None

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
        if self.mail_provider == "ses" and not (self.ses_region or self.aws_region):
            problems.append("MAIL_PROVIDER=ses needs SES_REGION or AWS_REGION")
        if self.storage_backend == "s3":
            if not self.s3_bucket:
                problems.append("STORAGE_BACKEND=s3 needs S3_BUCKET")
            if not (self.s3_region or self.aws_region or self.s3_endpoint_url):
                problems.append("STORAGE_BACKEND=s3 needs S3_REGION or AWS_REGION")
        if self.public_scheme != "https":
            problems.append("PUBLIC_SCHEME must be https")
        if self.flower_enabled and not self.flower_basic_auth:
            # Flower is not published, but "on a private network" is not a
            # credential. Without this, anything else on that network reads
            # every job's arguments.
            problems.append("FLOWER_BASIC_AUTH must be set, or FLOWER_ENABLED turned off")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
