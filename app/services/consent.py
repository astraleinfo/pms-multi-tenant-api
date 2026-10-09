"""Consent forms and patients' consents — every rule lives here.

Versions: a form has at most one `draft` and one `published` version. Editing
never touches a published version — it edits the draft, which is made from the
published content (or an older version, or the template) on first edit.
Publishing turns the draft into the published version and archives the one it
replaces. Archived and published versions are frozen; a trigger holds that too.

Consents: asking a patient copies the published version's content, the
patient's details and the clinician onto the record, so the record is exactly
what the patient saw however the form changes later. Signing or declining
makes it final; `expired` is read off the clock, never written.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.clinical import Patient
from app.models.consent import ConsentForm, ConsentFormVersion, PatientConsent
from app.models.enums import ConsentStatus, ConsentVersionStatus
from app.models.identity import User
from app.models.tenancy import Tenant
from app.schemas.consent import ConsentContent, PatientConsentIn, SignIn
from app.services import consent_templates

DRAFT, PUBLISHED, ARCHIVED = (
    ConsentVersionStatus.DRAFT,
    ConsentVersionStatus.PUBLISHED,
    ConsentVersionStatus.ARCHIVED,
)

#: a signature is a small PNG; anything larger is not one drawn on the screen
MAX_SIGNATURE_BYTES = 300_000
_PNG = b"\x89PNG\r\n\x1a\n"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _status(value: object) -> str:
    return str(getattr(value, "value", value))


# --- forms ----------------------------------------------------------------------------


async def list_forms(session: AsyncSession, tenant_id: uuid.UUID) -> list[ConsentForm]:
    rows = await session.execute(
        select(ConsentForm).where(ConsentForm.tenant_id == tenant_id).order_by(ConsentForm.created_at)
    )
    return list(rows.scalars())


async def ensure_default(session: AsyncSession, tenant: Tenant) -> list[ConsentForm]:
    """The clinic's forms — and, for a clinic that has none yet, the default one
    for its clinical type, made and published from the system template."""
    forms = await list_forms(session, tenant.id)
    if forms:
        return forms
    template = consent_templates.default_for(tenant.clinical_type)
    form = await create_form(session, tenant, template.key, actor_label="System", actor_id=None)
    return [form]


async def get_form(session: AsyncSession, tenant_id: uuid.UUID, form_id: uuid.UUID) -> ConsentForm:
    form = (
        await session.execute(
            select(ConsentForm).where(ConsentForm.tenant_id == tenant_id, ConsentForm.id == form_id)
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFound("No such consent form in this clinic.")
    return form


async def create_form(
    session: AsyncSession,
    tenant: Tenant,
    template_key: str,
    *,
    actor_label: str,
    actor_id: uuid.UUID | None,
) -> ConsentForm:
    """The clinic's own copy of a system template, published as v1 so it can be
    used straight away. The template is copied, never referenced."""
    template = consent_templates.template(template_key)
    if template is None:
        raise NotFound("No such consent template.")
    form = ConsentForm(
        tenant_id=tenant.id,
        template_key=template.key,
        name=template.label,
        created_by_id=actor_id,
        created_by_label=actor_label,
    )
    session.add(form)
    await session.flush()
    stamp = now()
    session.add(
        ConsentFormVersion(
            tenant_id=tenant.id,
            form_id=form.id,
            number=1,
            status=PUBLISHED,
            content=ConsentContent.model_validate(template.content).model_dump(),
            created_by_label=actor_label,
            updated_by_label=actor_label,
            published_by_label=actor_label,
            published_at=stamp,
        )
    )
    await session.flush()
    await session.refresh(form, ["versions"])
    return form


def version_with(form: ConsentForm, status: ConsentVersionStatus) -> ConsentFormVersion | None:
    return next((v for v in form.versions if _status(v.status) == status.value), None)


def version_numbered(form: ConsentForm, number: int) -> ConsentFormVersion:
    found = next((v for v in form.versions if v.number == number), None)
    if found is None:
        raise NotFound(f"This form has no version {number}.")
    return found


async def consent_counts(
    session: AsyncSession, tenant_id: uuid.UUID, form_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(PatientConsent.version_id, func.count())
        .where(PatientConsent.tenant_id == tenant_id, PatientConsent.form_id == form_id)
        .group_by(PatientConsent.version_id)
    )
    return {vid: n for vid, n in rows.all()}


async def _write_draft(
    session: AsyncSession,
    form: ConsentForm,
    content: dict,
    *,
    actor_label: str,
) -> ConsentFormVersion:
    """Put `content` in the form's draft — making the draft if there is none."""
    draft = version_with(form, DRAFT)
    if draft is None:
        draft = ConsentFormVersion(
            tenant_id=form.tenant_id,
            form_id=form.id,
            number=max((v.number for v in form.versions), default=0) + 1,
            status=DRAFT,
            content=content,
            created_by_label=actor_label,
            updated_by_label=actor_label,
        )
        session.add(draft)
    else:
        draft.content = content
        draft.updated_by_label = actor_label
        draft.updated_at = now()
    await session.flush()
    await session.refresh(form, ["versions"])
    return draft


async def start_draft(
    session: AsyncSession,
    form: ConsentForm,
    *,
    from_version: int | None,
    actor_label: str,
) -> ConsentFormVersion:
    """Begin editing. With no `from_version` an existing draft is simply
    returned; otherwise the draft is (re)filled from that version — which is
    how an older version is restored: as a new draft, never by reviving it."""
    draft = version_with(form, DRAFT)
    if from_version is None:
        if draft is not None:
            return draft
        source = version_with(form, PUBLISHED) or (form.versions[0] if form.versions else None)
        if source is None:
            raise NotFound("This form has no version to start from.")
    else:
        source = version_numbered(form, from_version)
    return await _write_draft(session, form, dict(source.content), actor_label=actor_label)


async def save_draft(
    session: AsyncSession,
    form: ConsentForm,
    content: ConsentContent,
    *,
    actor_label: str,
) -> ConsentFormVersion:
    return await _write_draft(session, form, content.model_dump(), actor_label=actor_label)


async def reset_to_template(
    session: AsyncSession, form: ConsentForm, *, actor_label: str
) -> ConsentFormVersion:
    template = consent_templates.template(form.template_key)
    if template is None:
        raise NotFound("The template this form was made from no longer exists.")
    content = ConsentContent.model_validate(template.content).model_dump()
    return await _write_draft(session, form, content, actor_label=actor_label)


async def discard_draft(session: AsyncSession, form: ConsentForm) -> None:
    draft = version_with(form, DRAFT)
    if draft is None:
        raise NotFound("There is no draft to discard.")
    await session.delete(draft)
    await session.flush()
    await session.refresh(form, ["versions"])


async def publish(session: AsyncSession, form: ConsentForm, *, actor_label: str) -> ConsentFormVersion:
    draft = version_with(form, DRAFT)
    if draft is None:
        raise Conflict("There is no draft to publish. Edit the form to start one.")
    ConsentContent.model_validate(draft.content)  # never publish a shape the signing screen cannot read
    stamp = now()
    current = version_with(form, PUBLISHED)
    if current is not None:
        current.status = ARCHIVED
        current.archived_at = stamp
        await session.flush()  # free the one-published slot before the draft takes it
    draft.status = PUBLISHED
    draft.published_at = stamp
    draft.published_by_label = actor_label
    draft.updated_by_label = actor_label
    form.name = draft.content.get("title") or form.name
    await session.flush()
    await session.refresh(form, ["versions"])
    return draft


# --- patients' consents ----------------------------------------------------------------


def effective_status(record: PatientConsent, at: datetime | None = None) -> str:
    status = _status(record.status)
    if status == ConsentStatus.PENDING.value and record.expires_at <= (at or now()):
        return ConsentStatus.EXPIRED.value
    return status


def patient_snapshot(patient: Patient) -> dict:
    return {
        "full_name": patient.full_name,
        "mrn": patient.mrn,
        "dob": patient.dob.isoformat() if patient.dob else None,
        "sex": _status(patient.sex),
        "phone": patient.phone or "",
        "email": patient.email or "",
        "address": patient.address or "",
    }


async def list_consents(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    patient_id: uuid.UUID | None = None,
) -> list[PatientConsent]:
    stmt = select(PatientConsent).where(PatientConsent.tenant_id == tenant_id)
    if patient_id is not None:
        stmt = stmt.where(PatientConsent.patient_id == patient_id)
    rows = await session.execute(stmt.order_by(PatientConsent.created_at.desc()))
    return list(rows.scalars())


async def get_consent(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    consent_id: uuid.UUID,
    *,
    signatures: bool = False,
) -> PatientConsent:
    stmt = select(PatientConsent).where(
        PatientConsent.tenant_id == tenant_id, PatientConsent.id == consent_id
    )
    if signatures:
        from sqlalchemy.orm import undefer

        stmt = stmt.options(
            undefer(PatientConsent.patient_signature), undefer(PatientConsent.provider_signature)
        )
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is None:
        raise NotFound("No such consent in this clinic.")
    return record


async def request_consent(
    session: AsyncSession,
    tenant: Tenant,
    payload: PatientConsentIn,
    *,
    patient: Patient,
    form: ConsentForm,
    actor: User,
    actor_label: str,
) -> PatientConsent:
    version = version_with(form, PUBLISHED)
    if version is None:
        raise Conflict("This form has no published version yet, so it cannot be used with patients.")
    content = ConsentContent.model_validate(version.content)

    known = {f.id: f for s in content.sections for f in s.fields}
    values = {k: v for k, v in payload.field_values.items() if k in known and v}
    missing = [f.label for f in known.values() if f.required and not values.get(f.id)]
    if missing:
        raise ValidationFailed(f"Fill in: {', '.join(missing)}.")

    provider: User | None = None
    if payload.provider_id:
        try:
            pid = uuid.UUID(payload.provider_id)
        except ValueError as exc:
            raise ValidationFailed("That clinician is not recognised.") from exc
        provider = (
            await session.execute(select(User).where(User.tenant_id == tenant.id, User.id == pid))
        ).scalar_one_or_none()
        if provider is None:
            raise NotFound("No such clinician in this clinic.")
    elif content.signatures.provider:
        provider = actor

    record = PatientConsent(
        tenant_id=tenant.id,
        patient_id=patient.id,
        form_id=form.id,
        version_id=version.id,
        version_number=version.number,
        content=content.model_dump(),
        patient_snapshot=patient_snapshot(patient),
        field_values=values,
        provider_id=provider.id if provider else None,
        provider_name=provider.name if provider else "",
        status=ConsentStatus.PENDING,
        requested_by_label=actor_label,
        expires_at=now() + timedelta(days=content.validity_days),
    )
    session.add(record)
    await session.flush()
    return record


def _png(data_url: str, what: str) -> bytes:
    head, _, body = data_url.partition(",")
    if not head.startswith("data:image/png;base64") or not body:
        raise ValidationFailed(f"The {what} must be drawn on the signing screen.")
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed(f"The {what} could not be read.") from exc
    if not raw.startswith(_PNG) or len(raw) > MAX_SIGNATURE_BYTES:
        raise ValidationFailed(f"The {what} could not be read.")
    return raw


def _open(record: PatientConsent) -> None:
    status = effective_status(record)
    if status == ConsentStatus.SIGNED.value:
        raise Conflict("This consent has already been signed, and cannot be changed.")
    if status == ConsentStatus.DECLINED.value:
        raise Conflict("This consent was declined, and cannot be changed. Request a new one.")
    if status == ConsentStatus.EXPIRED.value:
        raise Conflict("This consent request has expired. Request a new one.")


async def sign(
    session: AsyncSession, record: PatientConsent, payload: SignIn, *, actor_label: str
) -> PatientConsent:
    _open(record)
    content = ConsentContent.model_validate(record.content)
    statements = {a.id: a for s in content.sections for a in s.statements}
    ticked = [a for a in payload.acknowledged if a in statements]
    unticked = [a.text for a in statements.values() if a.required and a.id not in ticked]
    if unticked:
        raise ValidationFailed("Every required statement must be acknowledged before signing.")

    patient_png = _png(payload.patient_signature, "patient's signature")
    provider_png = None
    if content.signatures.provider:
        if not payload.provider_signature:
            raise ValidationFailed("The clinician's signature is required on this form.")
        provider_png = _png(payload.provider_signature, "clinician's signature")

    record.acknowledged = ticked
    record.signer_name = payload.signer_name.strip()
    record.signer_relationship = payload.signer_relationship.strip()
    record.patient_signature = patient_png
    record.provider_signature = provider_png
    record.witnessed_by_label = actor_label
    record.signed_at = now()
    record.status = ConsentStatus.SIGNED
    await session.flush()
    return record


async def decline(
    session: AsyncSession, record: PatientConsent, *, reason: str, actor_label: str
) -> PatientConsent:
    _open(record)
    record.status = ConsentStatus.DECLINED
    record.declined_at = now()
    record.decline_reason = reason.strip()
    record.witnessed_by_label = actor_label
    await session.flush()
    return record
