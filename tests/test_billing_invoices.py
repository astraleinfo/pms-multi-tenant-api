"""Invoice arithmetic, tax, and immutability.

Requirement 10 states the sum:

    Base Plan + Add-ons + Proration - Discounts/Credits + Taxes = Invoice Total

so the first test builds an invoice with one of each and checks that the stored
totals equal the sum of the stored lines — not that the code agrees with itself,
but that the document adds up.

Requirement 11 says tax must be configurable. The tax tests change the
`tax_rates` table and watch the invoice change with it; there is no assertion
anywhere that 18% is special, and the "no tax rows" case produces a zero tax
total rather than a crash.

Requirement 9 says invoices are immutable. That is enforced by a trigger and
two rules in migration 0009, so the tests attack the database directly — via
the ORM, as the application would — rather than through a service that could
be politely declining.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import session_factory, tenant_session
from app.models.billing import SubscriptionInvoice, SubscriptionInvoiceItem, TaxRate
from app.models.enums import InvoiceItemKind, SubscriptionInvoiceStatus, TaxScope
from app.services.payment import billing_service, subscription_service
from app.services.payment.addon import billing_service as addon_service
from app.services.payment.invoice import generation_service
from app.services.payment.tax import calculator as tax_calculator
from tests.billing_helpers import (
    LAB_MONTHLY,
    PHARMACY_MONTHLY,
    STARTER,
    invoices_of,
    make_tenant,
    plan,
)

#: The billing catalogue — plans, plan features and tax rates — is seeded, and
#: the seeder truncates first. Declaring the dependency means this module gets a
#: clean database whether it is run alone or with the rest of the suite; without
#: it, a rerun would find the previous run's payments still there.
pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)
FEB_1 = date(2026, 2, 1)


@asynccontextmanager
async def _owner_session():
    """A session as the migration/owner role.

    The catalogue belongs to the platform: the application role is deliberately
    denied INSERT on `plans` and `tax_rates` (migration 0009), so a test that
    configures pricing has to be whoever configures pricing in production — the
    same role the seeder uses.
    """
    from app.core.config import settings

    engine = create_async_engine(settings.migration_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                yield session
    finally:
        await engine.dispose()


async def _india(tenant_id, *, place_of_supply: str = "Karnataka") -> None:
    """Put the clinic where the seeded tax rows apply."""
    async with tenant_session(tenant_id) as session:
        account = await billing_service.get_account(session, tenant_id)
        account.country = "IN"
        account.place_of_supply = place_of_supply
        account.legal_name = "Billing Test Clinic"


def _sum_of(invoice, *kinds: InvoiceItemKind) -> Decimal:
    return sum(
        (Decimal(str(i.amount)) for i in invoice.items if str(i.kind) in {k.value for k in kinds}),
        Decimal("0"),
    )


async def test_the_invoice_total_is_the_sum_the_requirement_states():
    """One of every kind of line on one invoice, and the totals must match.

    Base plan (full), an add-on activated on day one (full), another activated
    mid-cycle (prorated) and carried into the next cycle, a credit from a plan
    change, and tax on the net.
    """
    tenant_id = await make_tenant("inv-sum")
    await _india(tenant_id)
    starter = await plan(STARTER)

    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "pharmacy", today=date(2026, 1, 15))

    # Build February's invoice with an explicit credit, so every line kind is
    # represented on one document.
    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        cycle = await billing_service.ensure_cycle(session, subscription, on=FEB_1)
        invoice, created = await generation_service.generate_for_cycle(
            session,
            subscription=subscription,
            cycle=cycle,
            today=FEB_1,
            credits=(generation_service.Credit(description="Goodwill", amount=Decimal("500.00")),),
        )
        assert created

    invoice = [i for i in await invoices_of(tenant_id) if i.period_start == FEB_1][0]

    charges = _sum_of(
        invoice, InvoiceItemKind.BASE_PLAN, InvoiceItemKind.ADDON, InvoiceItemKind.PRORATION
    )
    credits = -_sum_of(invoice, InvoiceItemKind.CREDIT, InvoiceItemKind.DISCOUNT)
    taxes = _sum_of(invoice, InvoiceItemKind.TAX)

    assert Decimal(str(invoice.subtotal)) == charges
    assert Decimal(str(invoice.discount_total)) == credits
    assert Decimal(str(invoice.tax_total)) == taxes
    # The requirement, exactly.
    assert Decimal(str(invoice.total)) == charges - credits + taxes

    # …and the charges are the plan plus both add-ons, at full price, because
    # February is a whole month for all three.
    assert charges == (
        Decimal(str(starter.amount)) + LAB_MONTHLY + PHARMACY_MONTHLY
    )
    assert credits == Decimal("500.00")


async def test_tax_is_read_from_the_table_and_split_into_its_components():
    """Requirement 11. Two 9% rows produce two tax lines, not one 18% line —
    and nothing in the code knows what CGST is."""
    tenant_id = await make_tenant("inv-tax")
    await _india(tenant_id)
    starter = await plan(STARTER)

    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )

    invoice = (await invoices_of(tenant_id))[0]
    tax_lines = [i for i in invoice.items if str(i.kind) == InvoiceItemKind.TAX.value]
    assert {i.tax_code for i in tax_lines} == {"CGST", "SGST"}, "an intra-state supply, split"

    base = Decimal(str(starter.amount))
    for line in tax_lines:
        assert Decimal(str(line.tax_percent)) == Decimal("9.000")
        assert Decimal(str(line.amount)) == (base * Decimal("9") / 100).quantize(Decimal("0.01"))

    # The tax total is the sum of the rounded components, which is the property
    # an auditor checks: the lines add up to the figure printed beside them.
    assert Decimal(str(invoice.tax_total)) == sum(Decimal(str(i.amount)) for i in tax_lines)
    assert Decimal(str(invoice.total)) == base + Decimal(str(invoice.tax_total))


async def test_a_clinic_outside_the_supplier_region_gets_the_inter_state_rate():
    """The same purchase, a different place of supply, a different tax shape.

    One 18% IGST line instead of two 9% ones — decided by the rows in
    `tax_rates`, which is where a jurisdiction's rules belong.
    """
    tenant_id = await make_tenant("inv-tax-igst")
    await _india(tenant_id, place_of_supply="Kerala")

    async with tenant_session(tenant_id) as session:
        # The platform bills from Karnataka; Kerala is therefore inter-state.
        from app.core.config import settings

        settings.billing_supplier_region = "Karnataka"
        try:
            starter = await plan(STARTER)
            await subscription_service.subscribe(
                session, tenant_id, plan=starter, today=JAN_1, trial_days=0
            )
        finally:
            settings.billing_supplier_region = ""

    invoice = (await invoices_of(tenant_id))[0]
    tax_lines = [i for i in invoice.items if str(i.kind) == InvoiceItemKind.TAX.value]
    assert [i.tax_code for i in tax_lines] == ["IGST"]
    assert Decimal(str(tax_lines[0].tax_percent)) == Decimal("18.000")


async def test_a_deployment_with_no_tax_rows_charges_no_tax():
    """Not a crash and not a hard-coded zero — simply no components to apply."""
    tenant_id = await make_tenant("inv-no-tax")
    starter = await plan(STARTER)

    # A country with no rows configured for it.
    async with tenant_session(tenant_id) as session:
        account = await billing_service.get_account(session, tenant_id)
        account.country = "GB"

    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )

    invoice = (await invoices_of(tenant_id))[0]
    assert [i for i in invoice.items if str(i.kind) == InvoiceItemKind.TAX.value] == []
    assert Decimal(str(invoice.tax_total)) == Decimal("0.00")
    assert Decimal(str(invoice.total)) == Decimal(str(starter.amount))


async def test_tax_is_charged_on_the_net_after_credits():
    """Credits come off before tax, and a credit larger than the charges
    produces no tax rather than negative tax."""
    async with session_factory() as session:
        rates = await tax_calculator.applicable_rates(
            session, on=JAN_1, country="IN", scope=TaxScope.INTRA_STATE
        )
        assert len(rates) == 2

        small = await tax_calculator.calculate(
            session,
            taxable_base=Decimal("1000.00"),
            on=JAN_1,
            country="IN",
            scope=TaxScope.INTRA_STATE,
        )
        assert small.total == Decimal("180.00")

        none_at_all = await tax_calculator.calculate(
            session,
            taxable_base=Decimal("-50.00"),
            on=JAN_1,
            country="IN",
            scope=TaxScope.INTRA_STATE,
        )
        assert none_at_all.total == Decimal("0.00")
        assert none_at_all.components == ()


async def test_a_rate_that_was_not_yet_effective_is_not_applied():
    """`effective_from` means a rate change is a new row, so an old invoice can
    still be explained."""
    async with session_factory() as session:
        future = await tax_calculator.applicable_rates(
            session, on=date(2019, 1, 1), country="IN", scope=TaxScope.INTRA_STATE
        )
        assert future == [], "the seeded rates start in 2020"


async def test_an_issued_invoices_figures_cannot_be_changed():
    """Requirement 9, enforced by the database rather than by a service.

    The trigger fires for the migration role too, so this is not something an
    application bug — or a console — can route around.
    """
    tenant_id = await make_tenant("inv-frozen")
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )
    invoice_id = (await invoices_of(tenant_id))[0].id

    with pytest.raises((DBAPIError, IntegrityError)) as caught:
        async with tenant_session(tenant_id) as session:
            row = await session.get(SubscriptionInvoice, invoice_id)
            row.total = Decimal("1.00")
            await session.flush()
    assert "cannot be changed" in str(caught.value)

    async with tenant_session(tenant_id) as session:
        row = await session.get(SubscriptionInvoice, invoice_id)
        assert Decimal(str(row.total)) == Decimal(str(starter.amount))


async def test_an_invoices_status_may_still_change():
    """Being paid is not being rewritten. The trigger freezes the money, not
    the lifecycle — otherwise nothing could ever be marked paid."""
    tenant_id = await make_tenant("inv-status")
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )
    invoice_id = (await invoices_of(tenant_id))[0].id

    async with tenant_session(tenant_id) as session:
        row = await session.get(SubscriptionInvoice, invoice_id)
        row.status = SubscriptionInvoiceStatus.UNCOLLECTIBLE
        row.payment_url = "https://rzp.io/i/example"
        await session.flush()

    async with tenant_session(tenant_id) as session:
        row = await session.get(SubscriptionInvoice, invoice_id)
        assert row.status == SubscriptionInvoiceStatus.UNCOLLECTIBLE
        assert row.payment_url == "https://rzp.io/i/example"


async def test_line_items_cannot_be_edited_or_deleted():
    """`DO INSTEAD NOTHING`, the same treatment `audit_events` gets.

    An UPDATE or DELETE is silently discarded rather than refused, which is why
    this reads the row back instead of expecting an exception.
    """
    tenant_id = await make_tenant("inv-lines-frozen")
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )
    invoice = (await invoices_of(tenant_id))[0]
    item_id = invoice.items[0].id
    original = Decimal(str(invoice.items[0].amount))

    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE subscription_invoice_items SET amount = 1 WHERE id = :id"),
            {"id": str(item_id)},
        )
        await session.execute(
            text("DELETE FROM subscription_invoice_items WHERE id = :id"), {"id": str(item_id)}
        )

    async with tenant_session(tenant_id) as session:
        row = await session.get(SubscriptionInvoiceItem, item_id)
        assert row is not None, "the delete was discarded"
        assert Decimal(str(row.amount)) == original, "and so was the update"


async def test_one_cycle_cannot_hold_two_live_invoices():
    """The duplicate-invoice guard, tested by going around the service.

    `generate_for_cycle` looks first, so proving the *index* holds means
    inserting a second invoice for the same cycle directly.
    """
    tenant_id = await make_tenant("inv-duplicate")
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        result = await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )
        cycle_id = result.cycle.id
        subscription_id = result.subscription.id

    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_id) as session:
            session.add(
                SubscriptionInvoice(
                    tenant_id=tenant_id,
                    subscription_id=subscription_id,
                    billing_cycle_id=cycle_id,
                    number="AST-DUPLICATE-1",
                    status=SubscriptionInvoiceStatus.ISSUED,
                    issued_on=JAN_1,
                    due_on=JAN_1,
                    period_start=JAN_1,
                    period_end=FEB_1,
                )
            )
            await session.flush()

    assert len(await invoices_of(tenant_id)) == 1


async def test_invoice_numbers_are_unique_across_the_platform():
    """They come from a PostgreSQL sequence, so two clinics billed at the same
    instant cannot collide and no number is ever reused."""
    numbers: set[str] = set()
    for index in range(3):
        tenant_id = await make_tenant(f"inv-number-{index}")
        starter = await plan(STARTER)
        async with tenant_session(tenant_id) as session:
            await subscription_service.subscribe(
                session, tenant_id, plan=starter, today=JAN_1, trial_days=0
            )
        invoice = (await invoices_of(tenant_id))[0]
        assert invoice.number.startswith("AST-202601-")
        numbers.add(invoice.number)
    assert len(numbers) == 3


async def test_the_application_role_cannot_write_the_catalogue():
    """Prices and tax rates are the platform's to set, not the API's.

    Migration 0009 revokes INSERT, UPDATE and DELETE on `plans`,
    `plan_features` and `tax_rates` from the application role, so a compromised
    request handler cannot give itself a discount. The seeder and migrations
    run as the owner and are unaffected.
    """
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    TaxRate(
                        code="SNEAKY",
                        name="Not allowed",
                        percent=Decimal("0"),
                        scope=TaxScope.ANY,
                        effective_from=date(2020, 1, 1),
                    )
                )


async def test_the_tax_catalogue_is_data_a_deployment_owns():
    """A new component, and the next invoice carries it. No code changes.

    Written as the owner role, which is who configures a catalogue — the
    application role is refused, as the test above shows.
    """
    tenant_id = await make_tenant("inv-tax-added")
    await _india(tenant_id)
    starter = await plan(STARTER)

    async with _owner_session() as session:
        session.add(
            TaxRate(
                code="CESS",
                name="Health cess",
                percent=Decimal("1.5"),
                scope=TaxScope.INTRA_STATE,
                country="IN",
                effective_from=date(2020, 1, 1),
            )
        )

    try:
        async with tenant_session(tenant_id) as session:
            await subscription_service.subscribe(
                session, tenant_id, plan=starter, today=JAN_1, trial_days=0
            )
        invoice = (await invoices_of(tenant_id))[0]
        codes = {i.tax_code for i in invoice.items if str(i.kind) == InvoiceItemKind.TAX.value}
        assert codes == {"CGST", "SGST", "CESS"}
        base = Decimal(str(starter.amount))
        expected = (base * Decimal("19.5") / 100).quantize(Decimal("0.01"))
        # Summed per component, so it may differ from taxing the whole 19.5% at
        # once by a cent — and the invoice's own lines are what must agree.
        assert abs(Decimal(str(invoice.tax_total)) - expected) <= Decimal("0.02")
    finally:
        # Leave the catalogue as the other tests expect to find it.
        async with _owner_session() as session:
            row = (
                await session.execute(select(TaxRate).where(TaxRate.code == "CESS"))
            ).scalar_one()
            await session.delete(row)
