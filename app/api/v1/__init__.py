"""Version 1 of the API."""

from fastapi import APIRouter

from app.api.v1 import (
    analytics,
    auth,
    catalog,
    consent,
    encounters,
    follow_ups,
    imports,
    jobs,
    messaging,
    modules,
    patients,
    platform,
    pricing,
    realtime,
    scheduling,
    subscription,
    tenant,
)

api_router = APIRouter(prefix="/v1")

# Order matters only for readability; every route is fully qualified.
api_router.include_router(auth.router)
api_router.include_router(catalog.router)
api_router.include_router(tenant.router)
api_router.include_router(subscription.clinic_router)
api_router.include_router(pricing.router)
api_router.include_router(patients.router)
api_router.include_router(consent.router)
api_router.include_router(scheduling.router)
api_router.include_router(follow_ups.router)
api_router.include_router(encounters.router)
api_router.include_router(encounters.prescriptions_router)
api_router.include_router(modules.pharmacy_router)
api_router.include_router(modules.lab_router)
api_router.include_router(modules.inventory_router)
api_router.include_router(modules.billing_router)
api_router.include_router(messaging.router)
api_router.include_router(analytics.router)
api_router.include_router(realtime.router)
api_router.include_router(platform.router)
api_router.include_router(subscription.console_router)
api_router.include_router(imports.router)
api_router.include_router(jobs.router)

__all__ = ["api_router"]
