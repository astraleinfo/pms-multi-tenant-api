"""The monthly bill each clinic pays the platform, and what happens if it doesn't.

The cycle, run by the beat (and by hand from the console):

1.  **Raise.** On the first run in a month, a clinic that has paid modules
    switched on gets a bill for that month: one line per module at today's
    catalogue price, due `RENT_DUE_DAYS` later. Nothing on, nothing billed.
2.  **Remind.** The clinic's admins are emailed as the bill moves along the
    ladder — raised, due soon, due today, overdue, last day — each rung once.
3.  **Hold.** A bill still open `RENT_GRACE_DAYS` after its due date puts the
    clinic on hold: its staff can sign in and see what is owed, and nothing
    else, until it is settled. Support sessions are never held.
4.  **Lift.** Settling (or voiding) the last overdue bill lifts the hold at
    once, from the console, without waiting for the next run.

Every step is idempotent. A bill is unique per clinic per month, a rung is
emailed only when the bill has not reached it yet, and the hold is recomputed
from the bills rather than toggled — so running the cycle twice in a minute,
or after a missed day, lands in the same place as running it once on time.

Dates are the clinic's own: "due today" means today where the clinic is.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.errors import Conflict, NotFound
from app.core.permissions import Role
from app.db.session import set_tenant
from app.models.enums import (
    RENT_STAGE_ORDER,
    AccountStatus,
    RentReminder,
    SubscriptionInvoiceStatus,
    TenantFeatureStatus,
    TenantStatus,
)
from app.models.identity import User
from app.models.rent import SubscriptionInvoice
from app.models.tenancy import Feature, Tenant, TenantConfig, TenantFeature
from app.schemas.common import zone
from app.services import mailer

log = logging.getLogger("pms.rent")

CENTS = Decimal("0.01")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clinic_today(tenant: Tenant) -> date:
    return datetime.now(zone(tenant.timezone)).date()


def month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    last = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return first, last


def hold_date(invoice: SubscriptionInvoice) -> date:
    """The first day the clinic is on hold if this bill is still open."""
    return invoice.due_on + timedelta(days=settings.rent_grace_days + 1)


def stage_for(invoice: SubscriptionInvoice, today: date) -> RentReminder:
    """Which rung of the ladder an open bill is on today."""
    late = (today - invoice.due_on).days
    grace = settings.rent_grace_days
    if late > grace:
        return RentReminder.BLOCKED
    if late > 0 and late == grace:
        return RentReminder.FINAL
    if late > 0:
        return RentReminder.OVERDUE
    if late == 0:
        return RentReminder.DUE
    if -late <= settings.rent_reminder_days_before_due:
        return RentReminder.DUE_SOON
    return RentReminder.ISSUED


def _further(stage: RentReminder | str, than: str) -> bool:
    return RENT_STAGE_ORDER[str(stage)] > RENT_STAGE_ORDER.get(than, 0)


# --- what a clinic is charged ---------------------------------------------------


async def billable_lines(session: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """One line per paid module switched on, at today's price. A trial is not
    billed; a core module has no price. The caller has the clinic bound."""
    rows = (
        await session.execute(
            select(Feature)
            .join(TenantFeature, TenantFeature.feature_key == Feature.key)
            .where(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.status == TenantFeatureStatus.ACTIVE,
                Feature.is_core.is_(False),
                Feature.price_monthly > 0,
            )
            .order_by(Feature.name)
        )
    ).scalars()
    return [
        {
            "feature_key": f.key,
            "name": f.name,
            "amount": float(Decimal(f.price_monthly).quantize(CENTS)),
        }
        for f in rows
    ]


async def raise_bill(
    session: AsyncSession, tenant: Tenant, today: date
) -> SubscriptionInvoice | None:
    """This month's bill for one clinic — the existing one, a new one, or None
    when there is nothing to bill. The caller has the clinic bound."""
    period_start, period_end = month_bounds(today)
    existing = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.tenant_id == tenant.id,
                SubscriptionInvoice.period_start == period_start,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    lines = await billable_lines(session, tenant.id)
    if not lines:
        return None
    total = sum((Decimal(str(line["amount"])) for line in lines), Decimal("0")).quantize(CENTS)
    bill = SubscriptionInvoice(
        tenant_id=tenant.id,
        number=f"AST-{period_start:%Y%m}-{tenant.slug.upper()}",
        period_start=period_start,
        period_end=period_end,
        currency=settings.rent_currency,
        amount=total,
        lines=lines,
        issued_on=today,
        due_on=today + timedelta(days=settings.rent_due_days),
        status=SubscriptionInvoiceStatus.OPEN,
        reminder_stage=RentReminder.NONE.value,
    )
    session.add(bill)
    await session.flush()
    return bill


async def list_bills(session: AsyncSession, tenant_id: uuid.UUID) -> list[SubscriptionInvoice]:
    return list(
        (
            await session.execute(
                select(SubscriptionInvoice)
                .where(SubscriptionInvoice.tenant_id == tenant_id)
                .order_by(SubscriptionInvoice.period_start.desc())
            )
        ).scalars()
    )


async def get_bill(
    session: AsyncSession, tenant_id: uuid.UUID, bill_id: uuid.UUID
) -> SubscriptionInvoice:
    bill = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.id == bill_id, SubscriptionInvoice.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if bill is None:
        raise NotFound("No such bill.")
    return bill


# --- who hears about it -----------------------------------------------------------


async def recipients(session: AsyncSession, tenant_id: uuid.UUID) -> list[User]:
    """The clinic's active practice admins — the people who can pay. The
    caller has the clinic bound."""
    return list(
        (
            await session.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.role == Role.TENANT_ADMIN.value,
                    User.status == AccountStatus.ACTIVE,
                    User.is_support.is_(False),
                )
            )
        ).scalars()
    )


def billing_url(tenant: Tenant) -> str:
    return f"{settings.tenant_origin(tenant.slug)}/settings?tab=subscription"


async def notices(
    session: AsyncSession, tenant: Tenant, bill: SubscriptionInvoice, stage: str
) -> list[mailer.Email]:
    """The messages one rung of the ladder sends: one per admin, built now
    (the data is read inside the caller's transaction) and sent by whoever
    calls. The caller has the clinic bound."""
    admins = await recipients(session, tenant.id)
    addresses = [(u.email, u.name) for u in admins if u.email]
    if not addresses:
        contact = (
            await session.execute(
                select(TenantConfig.contact_email).where(TenantConfig.tenant_id == tenant.id)
            )
        ).scalar_one_or_none()
        if contact:
            addresses = [(contact, tenant.name)]
    if not addresses:
        log.warning("no one to tell about bill %s at %s", bill.number, tenant.slug)
        return []

    return [
        mailer.rent_notice(
            to=address,
            name=name,
            clinic=tenant.name,
            stage=stage,
            number=bill.number,
            period=bill.period_start.strftime("%B %Y"),
            amount=f"{bill.currency} {float(bill.amount):,.2f}",
            lines=[(line["name"], f"{bill.currency} {float(line['amount']):,.2f}") for line in bill.lines],
            due_on=bill.due_on,
            hold_on=hold_date(bill),
            reference=bill.payment_reference,
            url=billing_url(tenant),
        )
        for address, name in addresses
    ]


async def deliver(emails: list[mailer.Email], *, what: str) -> int:
    """Send them all at once rather than one after another — each SMTP or SES
    hand-over is a network round trip of its own. Returns how many left."""

    async def one(email: mailer.Email) -> bool:
        try:
            await mailer.send(email)
            return True
        except mailer.MailError as exc:
            log.error("could not send %s to %s: %s", what, email.to, exc)
            return False

    results = await asyncio.gather(*(one(e) for e in emails))
    return sum(results)


async def _email(
    session: AsyncSession, tenant: Tenant, bill: SubscriptionInvoice, stage: str
) -> bool:
    """Email every admin about one bill. True if at least one message left."""
    emails = await notices(session, tenant, bill, stage)
    return bool(emails) and await deliver(emails, what=f"{stage} notice for {bill.number}") > 0


# --- the hold -----------------------------------------------------------------------


async def _overdue_past_grace(session: AsyncSession, tenant: Tenant, today: date) -> bool:
    bills = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.tenant_id == tenant.id,
                SubscriptionInvoice.status == SubscriptionInvoiceStatus.OPEN,
            )
        )
    ).scalars()
    return any(today >= hold_date(b) for b in bills)


async def reconcile_hold(session: AsyncSession, tenant: Tenant, today: date) -> str | None:
    """Put the clinic on hold, or lift it, to match its bills. Returns what
    changed: "held", "lifted" or None. The caller has the clinic bound."""
    should = (
        not tenant.billing_exempt
        and tenant.status != TenantStatus.SUSPENDED
        and await _overdue_past_grace(session, tenant, today)
    )
    if should and tenant.billing_hold_since is None:
        tenant.billing_hold_since = _now()
        await session.flush()
        return "held"
    if not should and tenant.billing_hold_since is not None:
        tenant.billing_hold_since = None
        await session.flush()
        return "lifted"
    return None


# --- one clinic, one run ------------------------------------------------------------


@dataclass
class ClinicOutcome:
    raised: str | None = None
    reminded: list[str] = field(default_factory=list)
    hold: str | None = None


async def cycle_for_tenant(session: AsyncSession, tenant: Tenant, today: date) -> ClinicOutcome:
    """Raise, remind and hold for one clinic. The caller has the clinic bound
    and commits."""
    outcome = ClinicOutcome()
    if tenant.billing_exempt or tenant.status == TenantStatus.SUSPENDED:
        # An exempt clinic is never held; a suspended one is offline already.
        outcome.hold = await reconcile_hold(session, tenant, today)
        return outcome

    existing_period = month_bounds(today)[0]
    before = (
        await session.execute(
            select(SubscriptionInvoice.id).where(
                SubscriptionInvoice.tenant_id == tenant.id,
                SubscriptionInvoice.period_start == existing_period,
            )
        )
    ).scalar_one_or_none()
    bill = await raise_bill(session, tenant, today)
    if bill is not None and before is None:
        outcome.raised = bill.number

    open_bills = (
        await session.execute(
            select(SubscriptionInvoice)
            .where(
                SubscriptionInvoice.tenant_id == tenant.id,
                SubscriptionInvoice.status == SubscriptionInvoiceStatus.OPEN,
            )
            .order_by(SubscriptionInvoice.due_on)
        )
    ).scalars().all()

    for open_bill in open_bills:
        target = stage_for(open_bill, today)
        if not _further(target, open_bill.reminder_stage):
            continue
        # Only the rung it is on now is emailed: a bill found three rungs on
        # after a missed week gets one message, not three.
        if await _email(session, tenant, open_bill, target.value):
            open_bill.reminder_stage = target.value
            open_bill.reminders_sent += 1
            open_bill.last_reminded_at = _now()
            outcome.reminded.append(f"{open_bill.number}:{target.value}")
        elif target == RentReminder.BLOCKED:
            # The hold does not wait on the mail server: a clinic nobody could
            # email is still a clinic that has not paid.
            open_bill.reminder_stage = target.value

    await session.flush()
    outcome.hold = await reconcile_hold(session, tenant, today)
    return outcome


async def run_cycle(
    factory: async_sessionmaker[AsyncSession],
    *,
    today: date | None = None,
    tenant_ids: list[uuid.UUID] | None = None,
) -> dict:
    """The whole platform, one clinic per transaction — a failure at one clinic
    is logged and does not stop the rest."""
    async with factory() as session:
        statement = select(Tenant.id)
        if tenant_ids:
            statement = statement.where(Tenant.id.in_(tenant_ids))
        ids = list((await session.execute(statement.order_by(Tenant.slug))).scalars())

    summary = {"clinics": 0, "raised": [], "reminded": [], "held": [], "lifted": [], "failed": []}
    # Clinics are independent — each has its own transaction and its own
    # admins — so they run side by side, a few at a time. One after another,
    # a run was a queue of mail-server round trips.
    gate = asyncio.Semaphore(5)

    async def one(tenant_id: uuid.UUID) -> tuple[uuid.UUID, str, ClinicOutcome] | uuid.UUID | None:
        async with gate:
            try:
                async with factory() as session, session.begin():
                    await set_tenant(session, tenant_id)
                    tenant = await session.get(Tenant, tenant_id)
                    if tenant is None:
                        return None
                    slug = tenant.slug
                    outcome = await cycle_for_tenant(session, tenant, today or clinic_today(tenant))
                return tenant_id, slug, outcome
            except Exception:  # noqa: BLE001 - one clinic must not stop the run
                log.exception("billing cycle failed for tenant %s", tenant_id)
                return tenant_id

    for result in await asyncio.gather(*(one(t) for t in ids)):
        if result is None:
            continue
        if not isinstance(result, tuple):
            summary["failed"].append(str(result))
            continue
        _, slug, outcome = result
        summary["clinics"] += 1
        if outcome.raised:
            summary["raised"].append(outcome.raised)
        summary["reminded"] += outcome.reminded
        if outcome.hold == "held":
            summary["held"].append(slug)
        elif outcome.hold == "lifted":
            summary["lifted"].append(slug)
    return summary


# --- settling, from the console --------------------------------------------------------


async def settle(
    session: AsyncSession, tenant: Tenant, bill_id: uuid.UUID, *, reference: str, by: str
) -> tuple[SubscriptionInvoice, str | None, list[mailer.Email]]:
    """Record a bill as paid and lift the hold if it was the last thing holding
    the clinic. The caller has the clinic bound.

    The receipts are returned, not sent: a payment is recorded whether or not
    the mail server is quick, so the caller sends them once the response has
    gone (see `deliver`)."""
    bill = await get_bill(session, tenant.id, bill_id)
    if bill.status != SubscriptionInvoiceStatus.OPEN:
        raise Conflict(f"That bill is already {bill.status.value}.")
    bill.status = SubscriptionInvoiceStatus.PAID
    bill.paid_at = _now()
    bill.payment_reference = reference.strip()
    bill.settled_by = by[:160]
    await session.flush()
    change = await reconcile_hold(session, tenant, clinic_today(tenant))
    receipts = await notices(session, tenant, bill, "paid")
    return bill, change, receipts


async def void(
    session: AsyncSession, tenant: Tenant, bill_id: uuid.UUID, *, by: str
) -> tuple[SubscriptionInvoice, str | None]:
    """Withdraw a bill raised in error. It stops counting towards the hold."""
    bill = await get_bill(session, tenant.id, bill_id)
    if bill.status != SubscriptionInvoiceStatus.OPEN:
        raise Conflict(f"That bill is already {bill.status.value}.")
    bill.status = SubscriptionInvoiceStatus.VOID
    bill.settled_by = by[:160]
    await session.flush()
    change = await reconcile_hold(session, tenant, clinic_today(tenant))
    return bill, change


def summary_for(tenant: Tenant, bills: list[SubscriptionInvoice], today: date) -> dict:
    """What the clinic owes, for its banner and its bills page."""
    open_bills = [b for b in bills if b.status == SubscriptionInvoiceStatus.OPEN]
    outstanding = sum((Decimal(b.amount) for b in open_bills), Decimal("0"))
    next_bill = min(open_bills, key=lambda b: b.due_on, default=None)
    return {
        "on_hold": tenant.billing_hold_since is not None,
        "hold_since": tenant.billing_hold_since.isoformat() if tenant.billing_hold_since else None,
        "exempt": tenant.billing_exempt,
        "currency": settings.rent_currency,
        "outstanding": float(outstanding),
        "open_count": len(open_bills),
        "next_due_on": next_bill.due_on.isoformat() if next_bill else None,
        "next_hold_on": hold_date(next_bill).isoformat() if next_bill else None,
        "overdue": bool(next_bill and today > next_bill.due_on),
        "due_days": settings.rent_due_days,
        "grace_days": settings.rent_grace_days,
    }
