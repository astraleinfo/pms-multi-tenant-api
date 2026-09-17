"""SendInvoiceEmailJob — email each new invoice to the clinic's administrators,
with a Pay Now link (requirement 12).

Idempotent by `subscription_invoices.emailed_at`: the column is stamped in the
same transaction as the send, so a job that runs twice sends once. A *reminder*
is therefore a deliberate act — `force=True` — rather than something a retry
can do by accident, which is the behaviour a practice manager wants from a
system that has their email address.

The send itself can fail without the stamp being written, because both are in
one transaction: a bounced SMTP connection rolls back the stamp and the invoice
is picked up again on the next sweep.

Where the Pay Now button points, in order of preference: the gateway's hosted
invoice page when one has been minted, and the clinic's own billing screen
otherwise. Both are built from configuration — never from the `Host` of
whatever triggered the send.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import Role
from app.jobs._base import JobResult, for_each_tenant
from app.models.billing import SubscriptionInvoice
from app.models.enums import AccountStatus, SubscriptionInvoiceStatus
from app.models.identity import User
from app.models.tenancy import Tenant
from app.services import mailer
from app.services.payment import billing_service
from app.services.payment.money import now

log = logging.getLogger("pms.billing.jobs")

JOB = "SendInvoiceEmailJob"

#: Statuses worth emailing. A draft is not finished and a void one never
#: happened; a paid one is emailed as a receipt on the first send only.
_SENDABLE = (
    SubscriptionInvoiceStatus.ISSUED,
    SubscriptionInvoiceStatus.PARTIALLY_PAID,
    SubscriptionInvoiceStatus.PAID,
)


async def recipients(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """Where an invoice goes.

    The billing account's own address when one has been set — a practice that
    wants invoices at accounts@ rather than at whoever happens to administer
    the software. Otherwise every active tenant administrator, because an
    invoice nobody receives is an invoice nobody pays.
    """
    account = await billing_service.get_account(session, tenant_id)
    if account.billing_email:
        return [account.billing_email]

    rows = (
        await session.execute(
            select(User.email).where(
                User.tenant_id == tenant_id,
                User.role == Role.TENANT_ADMIN.value,
                User.status == AccountStatus.ACTIVE,
                User.is_support.is_(False),
            )
        )
    ).scalars()
    return sorted({email for email in rows if email})


def pay_url(invoice: SubscriptionInvoice, tenant: Tenant) -> str:
    """The Pay Now target. Never carries a token — the recipient signs in."""
    if invoice.payment_url:
        return invoice.payment_url
    origin = settings.tenant_origin(tenant.slug)
    return f"{origin}{settings.billing_portal_path}"


async def _send(session: AsyncSession, tenant_id: uuid.UUID, today: date, *, force: bool) -> int:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:  # pragma: no cover - the registry row is always there
        return 0

    stmt = select(SubscriptionInvoice).where(
        SubscriptionInvoice.tenant_id == tenant_id,
        SubscriptionInvoice.status.in_(_SENDABLE),
    )
    if not force:
        stmt = stmt.where(SubscriptionInvoice.emailed_at.is_(None))
    invoices = list((await session.execute(stmt.order_by(SubscriptionInvoice.issued_on))).scalars())
    if not invoices:
        return 0

    to = await recipients(session, tenant_id)
    if not to:
        log.warning("tenant %s has no invoice recipient; leaving invoices unsent", tenant_id)
        return 0

    sent = 0
    for invoice in invoices:
        balance = billing_service.balance_of(invoice)
        message = mailer.subscription_invoice(
            to=", ".join(to),
            clinic=tenant.name,
            number=invoice.number,
            # The last day *inside* the period. The stored `period_end` is
            # exclusive, and "1 Feb to 1 Mar" on an invoice for February
            # invites the reasonable question of who is paying for 1 March.
            period=(
                f"{invoice.period_start:%d %b %Y} to "
                f"{invoice.period_end - timedelta(days=1):%d %b %Y}"
            ),
            currency=str(invoice.currency),
            total=f"{invoice.total:.2f}",
            balance=f"{balance:.2f}",
            due_on=f"{invoice.due_on:%d %b %Y}",
            pay_url=pay_url(invoice, tenant),
            lines=[
                (item.description, f"{str(invoice.currency)} {item.amount:,.2f}")
                for item in invoice.items
            ],
        )
        # Stamped in the same transaction as the send: a failed handover rolls
        # the stamp back and the invoice is picked up again next sweep.
        await mailer.send(message)
        invoice.emailed_at = now()
        sent += 1

    await session.flush()
    return sent


async def run(tenant_id: uuid.UUID | None = None, *, force: bool = False) -> JobResult:
    """Send unsent invoices. `force=True` resends everything — a reminder,
    which is a deliberate act rather than something a retry can cause."""

    async def work(session: AsyncSession, clinic_id: uuid.UUID, today: date) -> int:
        return await _send(session, clinic_id, today, force=force)

    return await for_each_tenant(JOB, work, tenant_id=tenant_id, only_active=True)
