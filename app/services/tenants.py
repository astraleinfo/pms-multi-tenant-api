"""Tenant lookup, entitlements, configuration, and the field-resolution engine.

The four axes stay separate here on purpose: `is_feature_active` never reads
`clinical_type`, and `resolve_schema` never reads an entitlement.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.enums import FieldEntity, FieldScope, FieldType, TenantFeatureStatus, TenantStatus
from app.models.tenancy import Feature, FieldDefinition, Tenant, TenantConfig, TenantFeature
from app.services import storage

CORE_FEATURES = ("patient", "appointment", "queue", "consultation", "prescription")

_SCOPE_RANK = {FieldScope.COMMON: 0, FieldScope.CLINICAL_TYPE: 1, FieldScope.TENANT: 2}


async def tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    return (
        await session.execute(select(Tenant).where(Tenant.slug == slug.lower()))
    ).scalar_one_or_none()


async def tenant_by_id(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    return await session.get(Tenant, tenant_id)


async def require_active_tenant(session: AsyncSession, slug: str) -> Tenant:
    tenant = await tenant_by_slug(session, slug)
    if tenant is None:
        raise NotFound("This address does not belong to a clinic.")
    if tenant.status == TenantStatus.SUSPENDED:
        raise NotFound("This clinic is suspended.")
    return tenant


# --- entitlements (§3.2) ----------------------------------------------------


async def list_features(session: AsyncSession, tenant_id: uuid.UUID) -> list[TenantFeature]:
    rows = await session.execute(
        select(TenantFeature).where(TenantFeature.tenant_id == tenant_id).order_by(TenantFeature.feature_key)
    )
    return list(rows.scalars())


async def active_feature_keys(session: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    rows = await session.execute(
        select(TenantFeature.feature_key).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.status == TenantFeatureStatus.ACTIVE,
        )
    )
    return set(rows.scalars())


async def is_feature_active(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> bool:
    row = await session.execute(
        select(TenantFeature.id).where(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature_key == key,
            TenantFeature.status == TenantFeatureStatus.ACTIVE,
        )
    )
    return row.scalar_one_or_none() is not None


async def set_feature(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, *, active: bool, today: date
) -> TenantFeature:
    feature = await session.get(Feature, key)
    if feature is None:
        raise NotFound("No such feature.")
    if feature.is_core and not active:
        raise Conflict("Core modules cannot be switched off.")

    row = (
        await session.execute(
            select(TenantFeature).where(
                TenantFeature.tenant_id == tenant_id, TenantFeature.feature_key == key
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = TenantFeature(tenant_id=tenant_id, feature_key=key)
        session.add(row)

    row.status = TenantFeatureStatus.ACTIVE if active else TenantFeatureStatus.INACTIVE
    row.activated_at = today if active else None
    await session.flush()
    return row


# --- configuration (§3.3) ---------------------------------------------------


async def get_config(session: AsyncSession, tenant_id: uuid.UUID) -> TenantConfig:
    row = (
        await session.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        from app.services import platform as platform_service  # deferred: platform.py imports this module too

        defaults = await platform_service.get_platform_settings(session)
        row = TenantConfig(
            tenant_id=tenant_id,
            day_start=defaults.default_day_start,
            day_end=defaults.default_day_end,
            slot_minutes=defaults.default_slot_minutes,
            default_duration=defaults.default_duration,
            tax_pct=defaults.default_tax_pct,
            payment_terms_days=defaults.default_payment_terms_days,
            expiry_alert_days=defaults.default_expiry_alert_days,
            currency=defaults.default_currency,
        )
        session.add(row)
        await session.flush()
    return row


async def lock_config(session: AsyncSession, tenant_id: uuid.UUID) -> TenantConfig:
    """Row-locked, so two front-desk machines cannot mint the same MRN,
    invoice number, or queue token."""
    await get_config(session, tenant_id)
    row = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id).with_for_update()
        )
    ).scalar_one()
    return row


async def next_mrn(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    config = await lock_config(session, tenant_id)
    config.mrn_sequence += 1
    return f"{config.mrn_prefix}-{config.mrn_sequence}"


async def next_invoice_number(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    config = await lock_config(session, tenant_id)
    config.invoice_sequence += 1
    return f"{config.invoice_prefix}-{config.invoice_sequence}"


# --- the clinic's logo (§3.3) ------------------------------------------------
#
# One image, not a collection — a clinic has at most one logo, so there is no
# id to name it by and no listing to page through. Same two-places-to-live
# split as a lab attachment (`app/services/storage.py`), except a fixed key
# rather than a minted one: each upload overwrites the object already there
# instead of leaving the old one to be cleaned up.

#: 2 MB. A clinic mark, not a scanned document — plenty for a crisp PNG or
#: JPEG wordmark, small enough that a mistaken upload cannot dent storage.
MAX_LOGO_BYTES = 2 * 1024 * 1024

ALLOWED_LOGO_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}


async def set_logo(
    session: AsyncSession, tenant_id: uuid.UUID, *, filename: str, content_type: str, data: bytes
) -> TenantConfig:
    if not data:
        raise ValidationFailed("That file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationFailed(
            f"Logos are limited to {MAX_LOGO_BYTES // (1024 * 1024)} MB.",
            details={"size_bytes": len(data), "limit_bytes": MAX_LOGO_BYTES},
        )

    kind = (content_type or "").split(";")[0].strip().lower()
    if kind not in ALLOWED_LOGO_CONTENT_TYPES:
        raise ValidationFailed(
            "A logo has to be a PNG, JPEG or WebP image.", details={"content_type": kind}
        )

    config = await lock_config(session, tenant_id)
    old_key = config.logo_storage_key

    key: str | None = None
    if storage.uses_s3():
        key = storage.logo_object_key(tenant_id=tenant_id)
        await storage.put(key, data, kind)

    config.logo_filename = filename.strip() or "logo"
    config.logo_content_type = kind
    config.logo_data = None if key else data
    config.logo_storage_key = key
    await session.flush()

    # The old object lives at the same key when both uploads used S3 — the
    # `put` above already overwrote it — but a clinic that just moved off S3
    # (or onto it) can leave one behind at a key the row no longer names.
    if old_key and old_key != key:
        storage.delete_after_commit(session, old_key)
    return config


async def config_with_logo(session: AsyncSession, tenant_id: uuid.UUID) -> TenantConfig:
    """The clinic's config with its logo's bytes loaded — the only read that
    does. `ctx.config`, loaded on every tenant request, leaves `logo_data`
    deferred so an unrelated call never drags an image across the wire."""
    row = (
        await session.execute(
            select(TenantConfig)
            .options(undefer(TenantConfig.logo_data))
            .where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return row if row is not None else await get_config(session, tenant_id)


async def logo_bytes(config: TenantConfig) -> bytes | None:
    """The clinic's logo, from wherever this row says it is — or `None` when
    it has not set one. `config` must come from `config_with_logo`, or a read
    held in `data` triggers a lazy load outside any greenlet."""
    if config.logo_filename is None:
        return None
    if config.logo_storage_key:
        return await storage.get(config.logo_storage_key)
    return config.logo_data


async def remove_logo(session: AsyncSession, config: TenantConfig) -> None:
    key = config.logo_storage_key
    config.logo_filename = None
    config.logo_content_type = None
    config.logo_data = None
    config.logo_storage_key = None
    await session.flush()
    if key:
        storage.delete_after_commit(session, key)


# --- field resolution (§3.3) ------------------------------------------------


async def field_definitions(
    session: AsyncSession, tenant_id: uuid.UUID, clinical_type: str | None = None
) -> list[FieldDefinition]:
    """Rows this tenant can see: the shared ones plus their own.

    Another clinic's custom fields are excluded here *and* by the row-level
    security policy on the table.
    """
    stmt = select(FieldDefinition).where(
        (FieldDefinition.tenant_id.is_(None)) | (FieldDefinition.tenant_id == tenant_id)
    )
    if clinical_type is not None:
        stmt = stmt.where(
            (FieldDefinition.clinical_type.is_(None)) | (FieldDefinition.clinical_type == clinical_type)
        )
    rows = await session.execute(stmt.order_by(FieldDefinition.order))
    return list(rows.scalars())


def resolve_schema(
    definitions: list[FieldDefinition], entity: FieldEntity, clinical_type: str, tenant_id: uuid.UUID
) -> list[FieldDefinition]:
    """common → clinical_type → tenant; later scope wins on `field_key`."""
    merged: dict[str, FieldDefinition] = {}
    for d in definitions:
        if d.entity != entity:
            continue
        if d.scope == FieldScope.CLINICAL_TYPE and d.clinical_type != clinical_type:
            continue
        if d.scope == FieldScope.TENANT and d.tenant_id != tenant_id:
            continue
        existing = merged.get(d.field_key)
        if existing is None or _SCOPE_RANK[FieldScope(d.scope)] >= _SCOPE_RANK[FieldScope(existing.scope)]:
            merged[d.field_key] = d
    return sorted(merged.values(), key=lambda d: (d.order, d.field_key))


async def effective_schema(
    session: AsyncSession, tenant: Tenant, entity: FieldEntity
) -> list[FieldDefinition]:
    definitions = await field_definitions(session, tenant.id, tenant.clinical_type)
    return resolve_schema(definitions, entity, tenant.clinical_type, tenant.id)


# --- dynamic value validation ------------------------------------------------

# Structured exam payloads. These are part of the product for a discipline —
# an FDI tooth chart, a refraction grid, a body-site lesion map — not custom
# fields a clinic added, so they are allowed through `clinical_ext` by name.
# Anything not listed here and not in the resolved schema is dropped.
STRUCTURED_EXAM_KEYS: dict[str, dict[str, type]] = {
    "dental": {"tooth_chart": dict, "procedures": list},
    "ophthalmology": {"refraction": dict, "va": dict},
    "dermatology": {"lesion_sites": list, "treatments": list},
}

_BUILT_IN = {
    FieldEntity.PATIENT: {
        "full_name", "dob", "sex", "phone", "email", "address", "blood_group", "allergies",
    },
    FieldEntity.ENCOUNTER: {"chief_complaint", "notes", "diagnosis", "follow_up_days"},
}


def validate_dynamic_values(
    schema: list[FieldDefinition],
    entity: FieldEntity,
    values: dict[str, Any],
    *,
    clinical_type: str | None = None,
) -> dict[str, Any]:
    """Check `clinical_ext` against the tenant's effective schema.

    Unknown keys are dropped rather than stored: a custom field is a
    configuration decision, and an arbitrary client-supplied key is not one.
    Dropping rather than rejecting also means a client cannot use this payload
    as free storage, or push a megabyte of JSON into a clinical record.
    """
    by_key = {d.field_key: d for d in schema if d.field_key not in _BUILT_IN[entity]}
    problems: list[dict[str, str]] = []
    cleaned: dict[str, Any] = {}

    for key, definition in by_key.items():
        raw = values.get(key)
        missing = raw is None or (isinstance(raw, str) and not raw.strip())
        if missing:
            if definition.required:
                problems.append({"field": key, "message": f"{definition.label} is required."})
            continue
        cleaned[key] = _coerce(definition, raw, problems)

    if entity == FieldEntity.ENCOUNTER and clinical_type:
        for key, expected in STRUCTURED_EXAM_KEYS.get(clinical_type, {}).items():
            if key in values and isinstance(values[key], expected):
                cleaned[key] = _sanitise_structured(values[key])

    if problems:
        raise ValidationFailed("Some fields need attention.", details={"fields": problems})
    return cleaned


def _sanitise_structured(value: Any, depth: int = 0) -> Any:
    """Keep a structured exam payload small, flat, and made of plain JSON."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        return {
            str(k)[:80]: _sanitise_structured(v, depth + 1)
            for k, v in list(value.items())[:200]
        }
    if isinstance(value, list):
        return [_sanitise_structured(v, depth + 1) for v in value[:200]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _coerce(definition: FieldDefinition, raw: Any, problems: list[dict[str, str]]) -> Any:
    kind = FieldType(definition.type)
    label = definition.label

    if kind == FieldType.NUMBER:
        if isinstance(raw, bool):
            problems.append({"field": definition.field_key, "message": f"{label} must be a number."})
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            problems.append({"field": definition.field_key, "message": f"{label} must be a number."})
            return None

    if kind == FieldType.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in {"true", "false"}:
            return raw.lower() == "true"
        problems.append({"field": definition.field_key, "message": f"{label} must be yes or no."})
        return None

    if kind == FieldType.SELECT:
        options = list(definition.options or [])
        if options and str(raw) not in options:
            problems.append({"field": definition.field_key, "message": f"{label} is not one of the options."})
            return None
        return str(raw)[:500]

    if kind == FieldType.MULTISELECT:
        options = set(definition.options or [])
        if not isinstance(raw, list):
            problems.append({"field": definition.field_key, "message": f"{label} must be a list."})
            return None
        chosen = [str(v)[:200] for v in raw[:50]]
        if options and any(v not in options for v in chosen):
            problems.append({"field": definition.field_key, "message": f"{label} has an unknown option."})
            return None
        return chosen

    if kind == FieldType.DATE:
        try:
            return date.fromisoformat(str(raw)).isoformat()
        except ValueError:
            problems.append({"field": definition.field_key, "message": f"{label} must be a date."})
            return None

    text = str(raw)
    limit = 20_000 if kind == FieldType.TEXTAREA else 500
    if len(text) > limit:
        problems.append({"field": definition.field_key, "message": f"{label} is too long."})
        return None
    return text


# --- what an account has put its name to ---------------------------------------------


async def records_by(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Every table row that names this account, counted by table.

    Read off the schema rather than listed by hand: any column with a foreign
    key to `users.id` is somewhere this person's name is on the record, and a
    table added next year is covered without anyone remembering to add it
    here. The caller has the clinic bound, so the counts are this clinic's.
    """
    from sqlalchemy import func, or_

    from app.db.base import Base

    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        columns = [
            fk.parent for fk in table.foreign_keys
            if fk.column.table.name == "users" and fk.column.name == "id"
        ]
        if not columns:
            continue
        found = (
            await session.execute(
                select(func.count()).select_from(table).where(or_(*(c == user_id for c in columns)))
            )
        ).scalar() or 0
        if found:
            counts[table.name] = int(found)
    return counts
