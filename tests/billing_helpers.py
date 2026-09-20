"""Fixtures and shorthands for the platform-billing tests.

Most billing behaviour is about *dates* — what a 28-day February does to a
prorated charge, what happens on the day a cycle turns over — and a date is not
something an HTTP test can choose. So the tests come in two shapes, and this
module supports both:

*   **service-level**, against a throwaway clinic created here, calling the
    billing services directly with an explicit `today`. That is how the month
    arithmetic is tested: by billing the same clinic through February, March
    and a leap year without waiting a year.
*   **over HTTP**, against the seeded clinics, for the API surface,
    authorisation and the shapes a browser actually receives.

`make_tenant` writes a real clinic — a `tenants` row, a config, and the same
`tenant_features` rows the console would create — so every test gets its own
and nothing leaks between them. **The seeded demo clinics are never touched**,
not even by the HTTP tests: the rest of the suite asserts on Northgate's
entitlements and Rowan's, and a billing test that subscribed one of them to a
plan bundling the lab would break half a dozen tests in three other files.
`make_admin` therefore gives a throwaway clinic its own administrator, so the
HTTP tests can sign in as one.

The slugs all begin `zzbt-` for a reason that looks arbitrary and is not:
`test_isolation` picks the two alphabetically-first clinics and asserts one of
them has patients. A test clinic sorting before `clearview` would be picked,
would have no patients, and would fail a test about row-level security for
reasons that have nothing to do with row-level security. Sorting last keeps
these clinics out of everybody else's way.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.session import session_factory, set_tenant, tenant_session
from app.models.billing import Plan, Subscription, SubscriptionInvoice, SubscriptionItem
from app.models.enums import TenantFeatureStatus, TenantStatus
from app.models.tenancy import Feature, Tenant, TenantConfig, TenantFeature
from app.services.tenants import CORE_FEATURES

#: Seeded plan prices, so a test asserts against the catalogue rather than
#: restating a number the catalogue could change under it.
STARTER = "starter"
PRACTICE = "practice"
CLINIC = "clinic"

#: Seeded add-on prices (`features.price_monthly`).
LAB_MONTHLY = Decimal("1999.00")
PHARMACY_MONTHLY = Decimal("2499.00")
INVENTORY_MONTHLY = Decimal("1499.00")

#: The seeded tax: 18% as CGST 9 + SGST 9 for an intra-state Indian supply.
GST_PERCENT = Decimal("18")


async def make_tenant(label: str, *, timezone: str = "Europe/London") -> uuid.UUID:
    """A clinic of this test's own, with core modules on and nothing bought.

    `tenants` is the platform registry and is written with no tenant bound,
    exactly as `platform_service.create_tenant` writes it; everything after
    that is written with the new clinic bound, so it goes through the same
    row-level policies as any other write.
    """
    slug = f"zzbt-{label}-{uuid.uuid4().hex[:8]}"[:40]
    async with session_factory() as session:
        async with session.begin():
            tenant = Tenant(
                name=f"Billing Test {label}",
                slug=slug,
                clinical_type="dental",
                status=TenantStatus.ACTIVE,
                city="Testbury",
                plan="Trial",
                since=date(2026, 1, 1),
                timezone=timezone,
            )
            session.add(tenant)
            await session.flush()
            tenant_id = tenant.id

            await set_tenant(session, tenant_id)
            session.add(TenantConfig(tenant_id=tenant_id))
            core = set(CORE_FEATURES)
            for feature in (await session.execute(select(Feature))).scalars():
                session.add(
                    TenantFeature(
                        tenant_id=tenant_id,
                        feature_key=feature.key,
                        status=(
                            TenantFeatureStatus.ACTIVE
                            if feature.key in core
                            else TenantFeatureStatus.INACTIVE
                        ),
                        activated_at=date(2026, 1, 1) if feature.key in core else None,
                    )
                )
    return tenant_id


#: The password every throwaway clinic's administrator gets. Hashed on the way
#: in like any other, and never anything but a test fixture.
ADMIN_PASSWORD = "billing-test-2026"  # noqa: S105


async def make_admin(tenant_id: uuid.UUID) -> tuple[str, str]:
    """Give a throwaway clinic an administrator, and return `(host, email)`.

    Needed only by the HTTP tests: `sign_in` goes through the real login route,
    which needs a real account on a real host. The role is `tenant_admin`
    because `SUBSCRIPTION_READ`/`SUBSCRIPTION_MANAGE` belong to that role alone
    — which is itself something the authorisation tests check.
    """
    from app.core.permissions import Role
    from app.core.security import hash_password
    from app.models.enums import AccountStatus
    from app.models.identity import User

    async with session_factory() as session:
        async with session.begin():
            tenant = await session.get(Tenant, tenant_id)
            await set_tenant(session, tenant_id)
            # `.test` and `.example` are special-use names and `email-validator`
            # refuses them, so the login route would 422 before it ever looked
            # at the password. `.clinic` is what the seeded accounts use.
            email = f"admin@{tenant.slug}.clinic"
            session.add(
                User(
                    tenant_id=tenant_id,
                    name="Billing Test Admin",
                    role=Role.TENANT_ADMIN.value,
                    email=email,
                    password_hash=hash_password(ADMIN_PASSWORD),
                    initials="BT",
                    status=AccountStatus.ACTIVE,
                )
            )
            host = f"{tenant.slug}.localhost"
    return host, email


async def plan(code: str) -> Plan:
    async with session_factory() as session:
        return (
            await session.execute(select(Plan).where(Plan.code == code))
        ).scalar_one()


async def subscription_of(tenant_id: uuid.UUID) -> Subscription | None:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                select(Subscription).where(Subscription.tenant_id == tenant_id)
            )
        ).scalars().first()


async def invoices_of(tenant_id: uuid.UUID) -> list[SubscriptionInvoice]:
    """Every invoice this clinic has, oldest first, line items loaded."""
    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(SubscriptionInvoice)
                .where(SubscriptionInvoice.tenant_id == tenant_id)
                .order_by(SubscriptionInvoice.created_at, SubscriptionInvoice.number)
            )
        ).scalars().all()
        for row in rows:
            await session.refresh(row, ["items", "payments"])
        return list(rows)


async def entitlement(tenant_id: uuid.UUID, feature_key: str) -> TenantFeature | None:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                select(TenantFeature).where(
                    TenantFeature.tenant_id == tenant_id,
                    TenantFeature.feature_key == feature_key,
                )
            )
        ).scalar_one_or_none()


async def item(tenant_id: uuid.UUID, feature_key: str) -> SubscriptionItem | None:
    """The live item for a module — the one that is not cancelled."""
    from app.models.enums import SubscriptionItemStatus

    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                select(SubscriptionItem).where(
                    SubscriptionItem.tenant_id == tenant_id,
                    SubscriptionItem.feature_key == feature_key,
                    SubscriptionItem.status != SubscriptionItemStatus.CANCELLED,
                )
            )
        ).scalar_one_or_none()


async def items_of(tenant_id: uuid.UUID) -> list[SubscriptionItem]:
    """Every item this clinic has ever had, cancelled ones included."""
    async with tenant_session(tenant_id) as session:
        return list(
            (
                await session.execute(
                    select(SubscriptionItem)
                    .where(SubscriptionItem.tenant_id == tenant_id)
                    .order_by(SubscriptionItem.feature_key, SubscriptionItem.created_at)
                )
            ).scalars()
        )


def lines_of(invoice: SubscriptionInvoice, kind: str | None = None) -> list:
    if kind is None:
        return list(invoice.items)
    return [item for item in invoice.items if str(item.kind) == kind]


def line_for(invoice: SubscriptionInvoice, feature_key: str):
    for item in invoice.items:
        if item.feature_key == feature_key:
            return item
    return None


def total_of(invoice: SubscriptionInvoice) -> Decimal:
    return Decimal(str(invoice.total))


__all__ = [
    "ADMIN_PASSWORD", "CLINIC", "GST_PERCENT", "make_admin", "INVENTORY_MONTHLY", "LAB_MONTHLY", "PHARMACY_MONTHLY",
    "PRACTICE", "STARTER", "entitlement", "invoices_of", "line_for", "lines_of", "make_tenant",
    "plan", "subscription_of", "total_of",
]
