"""The cart: modules a clinic means to buy together.

Server-side, for the same reason no request body in this API carries an
amount — a cart the client owns is a total the client can choose. What is
stored is a list of references, and `cart_items` has no money column at all:
the figures are worked out by `quote_service` on every read and again at
checkout, so a price that moved between the two is caught rather than honoured.

One open cart per clinic. That is `uq_carts_one_open_per_tenant`, a partial
unique index, rather than a service that looks first — two tabs pressing Add
to Cart at the same moment meet the index, not a race.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import Conflict, NotFound
from app.models.billing import Cart, CartItem, ModulePrice, Subscription, SubscriptionInvoice
from app.models.enums import CartStatus, PlanInterval, TenantFeatureStatus
from app.models.tenancy import Feature, TenantFeature
from app.services.payment import billing_service
from app.services.payment.money import now

log = logging.getLogger("pms.billing")


async def current(session: AsyncSession, tenant_id: uuid.UUID) -> Cart | None:
    """This clinic's open cart, or None when it has never started one."""
    return (
        await session.execute(
            select(Cart)
            .where(Cart.tenant_id == tenant_id, Cart.status == CartStatus.OPEN)
            .options(selectinload(Cart.items))
        )
    ).scalar_one_or_none()


async def open_cart(session: AsyncSession, tenant_id: uuid.UUID) -> Cart:
    """The open cart, started if there is not one yet."""
    cart = await current(session, tenant_id)
    if cart is not None:
        return cart

    subscription = await billing_service.require_subscription(session, tenant_id)
    plan = await billing_service.get_plan(session, subscription.plan_id)
    cart = Cart(tenant_id=tenant_id, status=CartStatus.OPEN, currency=plan.currency)
    session.add(cart)
    await session.flush()
    await session.refresh(cart, ["items"])
    return cart


async def active_price(
    session: AsyncSession, feature_key: str, *, interval: PlanInterval, currency
) -> ModulePrice:
    """The catalogue row this module is sold at today.

    Raises rather than falling back to the monthly figure when a yearly price
    has not been set: quietly billing a year at the monthly rate, or a month at
    the yearly one, is worse than saying the interval is not for sale.
    """
    price = (
        await session.execute(
            select(ModulePrice).where(
                ModulePrice.feature_key == feature_key,
                ModulePrice.interval == interval,
                ModulePrice.currency == currency,
                ModulePrice.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if price is None:
        raise NotFound("That module is not sold on that billing interval.")
    return price


async def add(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    interval: PlanInterval = PlanInterval.MONTHLY,
    today: date | None = None,
) -> Cart:
    """Put a module in the cart, or change the interval it is in the cart at.

    Only what is definitely wrong is refused here — a module that does not
    exist, one with no price on that interval, and one the clinic is already
    subscribed to. Everything else the quote reports line by line, because a
    cart has to be able to show a problem rather than refuse to open.

    Adding the same module twice replaces the line rather than adding a second:
    the lab monthly *and* the lab yearly is a contradiction, not two purchases,
    and `uq_cart_item_feature` says so.
    """
    feature = await session.get(Feature, feature_key)
    if feature is None:
        raise NotFound("No such module.")
    if feature.is_core:
        raise Conflict("That module is part of the core product and is not sold separately.")

    entitlement = (
        await session.execute(
            select(TenantFeature).where(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.feature_key == feature_key,
                TenantFeature.status == TenantFeatureStatus.ACTIVE,
                TenantFeature.cancel_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if entitlement is not None:
        raise Conflict("This clinic is already subscribed to that module.")

    cart = await open_cart(session, tenant_id)
    price = await active_price(session, feature_key, interval=interval, currency=cart.currency)

    existing = next((i for i in cart.items if i.feature_key == feature_key), None)
    if existing is not None:
        existing.interval = interval
        existing.price_id = price.id
    else:
        session.add(
            CartItem(
                tenant_id=tenant_id,
                cart_id=cart.id,
                feature_key=feature_key,
                price_id=price.id,
                interval=interval,
            )
        )
    await session.flush()
    await session.refresh(cart, ["items"])
    return cart


async def remove(session: AsyncSession, tenant_id: uuid.UUID, feature_key: str) -> Cart:
    """Take a module out. Taking out something that is not in is not an error."""
    cart = await open_cart(session, tenant_id)
    for item in list(cart.items):
        if item.feature_key == feature_key:
            await session.delete(item)
    await session.flush()
    await session.refresh(cart, ["items"])
    return cart


async def clear(session: AsyncSession, tenant_id: uuid.UUID) -> Cart:
    """Empty the cart without closing it."""
    cart = await open_cart(session, tenant_id)
    for item in list(cart.items):
        await session.delete(item)
    await session.flush()
    await session.refresh(cart, ["items"])
    return cart


async def mark_converted(
    session: AsyncSession, cart: Cart, invoice: SubscriptionInvoice | None
) -> None:
    """Close the cart against the one invoice it became.

    Kept rather than deleted: a converted cart is the only place the intent
    behind a multi-module invoice is written down — that these modules were
    bought *together*, on one payment, rather than five times in a minute.
    """
    cart.status = CartStatus.CONVERTED
    cart.converted_invoice_id = invoice.id if invoice is not None else None
    cart.converted_at = now()
    await session.flush()


async def subscription_for(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    return await billing_service.require_subscription(session, tenant_id, for_update=True)


__all__ = [
    "active_price",
    "add",
    "clear",
    "current",
    "mark_converted",
    "open_cart",
    "remove",
    "subscription_for",
]
