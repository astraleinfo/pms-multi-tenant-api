"""The shop window: every module, as the catalogue page shows it.

One read that answers the three questions a module's card has to answer at
once — what it is, what it costs, and whether this clinic already has it.
Doing it in one place keeps the page from making the second question up: a
module bundled by the plan has no price to show, and a zero there would read
as "free extra" rather than "part of what you already pay".

Nothing here writes, and nothing here is entitled to sell a module the
catalogue has no price for: `monthly` is None when no active `module_prices`
row exists, and the page is expected to say so rather than offer a Buy button
that cannot work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import ModuleListing, ModulePrice
from app.models.enums import (
    CartStatus,
    Currency,
    ModuleListingStatus,
    PlanInterval,
    TenantFeatureStatus,
)
from app.models.tenancy import Feature, TenantFeature
from app.services.payment import billing_service


@dataclass(frozen=True)
class ModuleCard:
    feature_key: str
    name: str
    summary: str
    is_core: bool

    headline: str
    long_description: str
    highlights: list
    video_url: str
    video_poster_url: str
    video_duration_s: int | None
    screenshots: list
    doc_links: list
    category: str
    badge: str
    sort_order: int

    currency: Currency
    #: None when the catalogue has no active price on that interval — which is
    #: the honest answer, and the reason the page can tell "not sold yearly"
    #: apart from "free".
    monthly: Decimal | None
    yearly: Decimal | None

    #: Bundled by the base plan: switched on, never invoiced separately.
    included: bool
    #: On right now, whatever is paying for it.
    subscribed: bool
    #: The day a scheduled cancellation takes effect, or None.
    cancels_on: date | None
    in_cart: bool

    @property
    def purchasable(self) -> bool:
        return (
            not self.is_core
            and not self.included
            and not self.subscribed
            and self.monthly is not None
        )


async def catalogue(session: AsyncSession, tenant_id: uuid.UUID) -> list[ModuleCard]:
    """Every published module, priced for this clinic's plan."""
    subscription = await billing_service.active_subscription(session, tenant_id)
    prices = (
        await billing_service.addon_prices(session, subscription.plan_id)
        if subscription is not None
        else {}
    )
    currency = (
        (await billing_service.get_plan(session, subscription.plan_id)).currency
        if subscription is not None
        else Currency.INR
    )

    features = {
        f.key: f for f in (await session.execute(select(Feature))).scalars()
    }
    listings = {
        listing.feature_key: listing
        for listing in (
            await session.execute(
                select(ModuleListing).where(
                    ModuleListing.status == ModuleListingStatus.PUBLISHED
                )
            )
        ).scalars()
    }
    catalogue_prices: dict[tuple[str, PlanInterval], Decimal] = {
        (row.feature_key, row.interval): row.amount
        for row in (
            await session.execute(
                select(ModulePrice).where(
                    ModulePrice.active.is_(True), ModulePrice.currency == currency
                )
            )
        ).scalars()
    }
    entitlements = {
        row.feature_key: row
        for row in (
            await session.execute(
                select(TenantFeature).where(TenantFeature.tenant_id == tenant_id)
            )
        ).scalars()
    }

    from app.models.billing import Cart, CartItem

    in_cart = set(
        (
            await session.execute(
                select(CartItem.feature_key)
                .join(Cart, Cart.id == CartItem.cart_id)
                .where(Cart.tenant_id == tenant_id, Cart.status == CartStatus.OPEN)
            )
        ).scalars()
    )

    cards: list[ModuleCard] = []
    for key, listing in listings.items():
        feature = features.get(key)
        if feature is None:  # pragma: no cover - FK prevents this
            continue
        price = prices.get(key)
        entitlement = entitlements.get(key)
        cards.append(
            ModuleCard(
                feature_key=key,
                name=feature.name,
                summary=feature.summary,
                is_core=feature.is_core,
                headline=listing.headline,
                long_description=listing.long_description,
                highlights=list(listing.highlights or []),
                video_url=listing.video_url,
                video_poster_url=listing.video_poster_url,
                video_duration_s=listing.video_duration_s,
                screenshots=list(listing.screenshots or []),
                doc_links=list(listing.doc_links or []),
                category=listing.category,
                badge=listing.badge,
                sort_order=listing.sort_order,
                currency=currency,
                monthly=catalogue_prices.get((key, PlanInterval.MONTHLY)),
                yearly=catalogue_prices.get((key, PlanInterval.YEARLY)),
                included=bool(price.included) if price is not None else feature.is_core,
                subscribed=(
                    entitlement is not None
                    and entitlement.status
                    in (TenantFeatureStatus.ACTIVE, TenantFeatureStatus.TRIAL)
                ),
                cancels_on=entitlement.cancel_at if entitlement is not None else None,
                in_cart=key in in_cart,
            )
        )

    cards.sort(key=lambda c: (c.sort_order, c.name))
    return cards


async def one(session: AsyncSession, tenant_id: uuid.UUID, feature_key: str) -> ModuleCard:
    """One module's page."""
    from app.core.errors import NotFound

    for card in await catalogue(session, tenant_id):
        if card.feature_key == feature_key:
            return card
    raise NotFound("No such module.")


__all__ = ["ModuleCard", "catalogue", "one"]
