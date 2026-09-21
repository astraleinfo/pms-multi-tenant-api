"""Test fixtures.

The suite runs against a real PostgreSQL database with the real migrations
applied, because the thing most worth testing here — row-level security — does
not exist in SQLite. The database is migrated and seeded once per session, and
each test signs in as a real user over HTTP.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Point the whole application at the test database before anything imports it.
TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://pms_app:app-password-change-me@localhost:55432/pms_test"
)
TEST_MIGRATION_DB = os.environ.get(
    "TEST_MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://pms_owner:owner-password-change-me@localhost:55432/pms_test",
)
os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": TEST_DB,
        "MIGRATION_DATABASE_URL": TEST_MIGRATION_DB,
        "JWT_SECRET": "test-secret-not-used-anywhere-real-000000",
        "APP_DOMAIN": "localhost",
        "COOKIE_SECURE": "false",
        "ALLOW_TENANT_HEADER": "false",
        "LOGIN_RATE_LIMIT": "1000/minute",
        "PASSWORD_RESET_RATE_LIMIT": "1000/minute",
        "API_RATE_LIMIT": "100000/minute",
        # No Flower in the suite. Point the reader at a port nothing listens on
        # so "the monitor is down" is instant and deterministic — which is the
        # state every guard test below wants anyway: the answer to "may I look
        # at this" must not depend on whether there is anything to look at.
        "FLOWER_URL": "http://127.0.0.1:9/",
        "FLOWER_TIMEOUT_SECONDS": "2",
        "FLOWER_BASIC_AUTH": "test:test",
    }
)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db import guard  # noqa: E402,F401
from app.main import app  # noqa: E402
from app.seed.loader import rid, seed_all  # noqa: E402

NORTHGATE = "northgate.localhost"
CLEARVIEW = "clearview.localhost"
ROWAN = "rowan.localhost"
#: The super-admin console has its own address. The apex is the product's
#: public face and answers for neither a clinic nor the console.
CONSOLE = "admin.localhost"
APEX = "localhost"

ACCOUNTS = {
    "northgate_admin": (NORTHGATE, "admin@northgate.clinic", "admin-2026"),
    "northgate_doctor": (NORTHGATE, "a.iyer@northgate.clinic", "aiyer-2026"),
    "northgate_reception": (NORTHGATE, "front@northgate.clinic", "front-2026"),
    "northgate_pharmacist": (NORTHGATE, "pharmacy@northgate.clinic", "pharmacy-2026"),
    "northgate_lab": (NORTHGATE, "lab@northgate.clinic", "lab-2026"),
    "clearview_admin": (CLEARVIEW, "admin@clearview.eye", "admin-2026"),
    "rowan_admin": (ROWAN, "owner@rowanskin.co", "owner-2026"),
}
#: Ines is a `super_admin`; Rafael is `support`, which is every part of the
#: console except the platform's own machinery.
PLATFORM = ("ines@astrale.health", "ines-2026")
PLATFORM_SUPPORT = ("rafael@astrale.health", "rafael-2026")


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    env = {**os.environ, "MIGRATION_DATABASE_URL": TEST_MIGRATION_DB}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, check=True, capture_output=True,
    )


@pytest.fixture(scope="session")
async def seeded(migrated_database) -> None:
    engine = create_async_engine(settings.migration_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await seed_all(session, reset=True)
    await engine.dispose()


@pytest.fixture
async def client(seeded) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


class Signed:
    """A signed-in client, pinned to one clinic's address."""

    def __init__(self, client: AsyncClient, host: str, token: str, body: dict) -> None:
        self._client = client
        self.host = host
        self.token = token
        self.user = body.get("user") or {}
        self.platform_user = body.get("platform_user") or {}
        self.tenant_id = body.get("tenant_id")

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Host": self.host, "Authorization": f"Bearer {self.token}"}
        headers.update(extra or {})
        return headers

    async def get(self, url: str, **kw):
        return await self._client.get(url, headers=self._headers(kw.pop("headers", None)), **kw)

    async def post(self, url: str, **kw):
        return await self._client.post(url, headers=self._headers(kw.pop("headers", None)), **kw)

    async def patch(self, url: str, **kw):
        return await self._client.patch(url, headers=self._headers(kw.pop("headers", None)), **kw)

    async def put(self, url: str, **kw):
        return await self._client.put(url, headers=self._headers(kw.pop("headers", None)), **kw)

    async def delete(self, url: str, **kw):
        return await self._client.delete(url, headers=self._headers(kw.pop("headers", None)), **kw)


async def patients_of(signed: Signed, **params) -> list[dict]:
    """The register is paged, so a test that wants rows asks for a page."""
    response = await signed.get("/v1/patients", params=params or None)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def sign_in(client: AsyncClient, key: str) -> Signed:
    host, email, password = ACCOUNTS[key]
    response = await client.post(
        "/v1/auth/login", headers={"Host": host}, json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return Signed(client, host, response.json()["access_token"], response.json())


@pytest.fixture
async def northgate(client) -> Signed:
    return await sign_in(client, "northgate_admin")


@pytest.fixture
async def northgate_doctor(client) -> Signed:
    return await sign_in(client, "northgate_doctor")


@pytest.fixture
async def clearview(client) -> Signed:
    return await sign_in(client, "clearview_admin")


@pytest.fixture
async def rowan(client) -> Signed:
    return await sign_in(client, "rowan_admin")


async def sign_in_console(client: AsyncClient, credentials=PLATFORM) -> Signed:
    email, password = credentials
    response = await client.post(
        "/v1/platform/auth/login", headers={"Host": CONSOLE}, json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return Signed(client, CONSOLE, response.json()["access_token"], response.json())


@pytest.fixture
async def console(client) -> Signed:
    return await sign_in_console(client)


@pytest.fixture
async def console_support(client) -> Signed:
    """Signed into the console, and not a super admin."""
    return await sign_in_console(client, PLATFORM_SUPPORT)


async def audit_lines(tenant_id: str, *, action: str | None = None) -> list[dict]:
    """Read the trail directly.

    There is no route that returns it — the audit table is written by the
    application and read by whoever operates it — so a test that wants to know
    what was recorded asks the database, with the clinic bound exactly as a
    request would bind it.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.db.session import tenant_session
    from app.models.tenancy import AuditEvent

    statement = select(AuditEvent).where(AuditEvent.tenant_id == _uuid.UUID(tenant_id))
    if action:
        statement = statement.where(AuditEvent.action == action)

    async with tenant_session(_uuid.UUID(tenant_id)) as session:
        rows = (await session.execute(statement.order_by(AuditEvent.at.desc()))).scalars().all()
        return [
            {
                "action": r.action,
                "actor_kind": r.actor_kind,
                "actor_label": r.actor_label,
                "entity": r.entity,
                "entity_id": r.entity_id,
                "outcome": r.outcome,
                "meta": r.meta,
            }
            for r in rows
        ]


def token_from(url: str) -> str:
    """The `token` query parameter of an invitation or reset link.

    Under the `console` mail transport the API hands the console the URL it
    would have emailed, which is how a test reads an inbox it does not have.
    """
    from urllib.parse import parse_qs, urlparse

    assert url, "no link was issued"
    return parse_qs(urlparse(url).query)["token"][0]


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from app.core.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


__all__ = [
    "rid", "sign_in", "patients_of", "Signed", "NORTHGATE", "CLEARVIEW", "ROWAN", "CONSOLE",
    "APEX", "token_from", "audit_lines",
]
