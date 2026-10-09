"""Consent forms (the clinic's, versioned) and patients' consents against them.

    /consent/templates                          the system templates — read-only
    /consent/forms                              the clinic's forms
    /consent/forms/{id}/draft                   start / save / discard the one draft
    /consent/forms/{id}/publish                 the draft becomes the published version
    /consent/forms/{id}/reset                   the draft becomes the template again
    /consent/forms/{id}/versions/{n}            one version, as content or as its document
    /consent/records                            patients' consents: ask, list
    /consent/records/{id}/sign | decline        the patient's answer — final either way

Every row is the clinic's own under row-level security; ids from another clinic
are simply not found. Changing a form needs `consent:manage`; asking a patient
and taking their signature needs `consent:request`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.deps import TenantContext, parse_uuid, require_permission
from app.core.errors import NotFound
from app.core.permissions import Permission
from app.models.consent import ConsentForm, ConsentFormVersion, PatientConsent
from app.schemas.consent import (
    ConsentContent,
    ConsentFormIn,
    ConsentFormOut,
    ConsentTemplateOut,
    ConsentVersionOut,
    DeclineIn,
    DraftContentIn,
    DraftIn,
    PatientConsentIn,
    PatientConsentOut,
    SignIn,
)
from app.services import audit, consent_templates
from app.services import consent as consent_service
from app.services import patients as patient_service
from app.services import tenants as tenant_service
from app.services.consent_pdf import ConsentPrint, build_consent_pdf

router = APIRouter(prefix="/consent", tags=["consent"])


async def _audit(ctx: TenantContext, action: str, entity: str, entity_id: object, **meta) -> None:
    await audit.record(
        ctx.session,
        action=action,
        tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id,
        actor_label=ctx.principal.label,
        entity=entity,
        entity_id=str(entity_id),
        meta=meta or None,
    )


def _version_out(v: ConsentFormVersion, counts: dict, *, content: bool = False) -> ConsentVersionOut:
    return ConsentVersionOut(
        id=str(v.id),
        number=v.number,
        status=str(getattr(v.status, "value", v.status)),
        created_by=v.created_by_label,
        created_at=v.created_at,
        updated_by=v.updated_by_label,
        updated_at=v.updated_at,
        published_by=v.published_by_label,
        published_at=v.published_at,
        archived_at=v.archived_at,
        consents=counts.get(v.id, 0),
        content=ConsentContent.model_validate(v.content) if content else None,
    )


async def _form_out(ctx: TenantContext, form: ConsentForm, *, full: bool = False) -> ConsentFormOut:
    counts = await consent_service.consent_counts(ctx.session, ctx.tenant_id, form.id)
    published = consent_service.version_with(form, consent_service.PUBLISHED)
    draft = consent_service.version_with(form, consent_service.DRAFT)
    template = consent_templates.template(form.template_key)
    return ConsentFormOut(
        id=str(form.id),
        name=form.name,
        template_key=form.template_key,
        template_label=template.label if template else form.template_key,
        created_by=form.created_by_label,
        created_at=form.created_at,
        published=_version_out(published, counts, content=full) if published else None,
        draft=_version_out(draft, counts, content=full) if draft else None,
        versions=[_version_out(v, counts) for v in form.versions] if full else [],
    )


def _pdf(content: bytes, filename: str) -> Response:
    safe = "".join(ch for ch in filename if ch.isalnum() or ch in "-_.") or "consent.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe}"', "Cache-Control": "private, no-store"},
    )


# --- templates ------------------------------------------------------------------------


@router.get("/templates", response_model=list[ConsentTemplateOut])
async def list_templates(
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> list[ConsentTemplateOut]:
    """The platform's templates. Read-only: a clinic copies one, it never edits one."""
    return [
        ConsentTemplateOut(key=t.key, label=t.label, clinical_type=t.clinical_type, description=t.description)
        for t in consent_templates.TEMPLATES.values()
    ]


@router.get("/templates/{key}", response_model=ConsentTemplateOut)
async def get_template(
    key: str,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> ConsentTemplateOut:
    t = consent_templates.template(key)
    if t is None:
        raise NotFound("No such consent template.")
    return ConsentTemplateOut(
        key=t.key,
        label=t.label,
        clinical_type=t.clinical_type,
        description=t.description,
        content=ConsentContent.model_validate(t.content),
    )


# --- the clinic's forms -----------------------------------------------------------------


@router.get("/forms", response_model=list[ConsentFormOut])
async def list_forms(
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_READ)),
) -> list[ConsentFormOut]:
    """The clinic's forms. A clinic that has none yet is given its default —
    the template for its clinical type, published as v1."""
    forms = await consent_service.ensure_default(ctx.session, ctx.tenant)
    return [await _form_out(ctx, f) for f in forms]


@router.post("/forms", response_model=ConsentFormOut, status_code=201)
async def create_form(
    payload: ConsentFormIn,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> ConsentFormOut:
    form = await consent_service.create_form(
        ctx.session,
        ctx.tenant,
        payload.template_key,
        actor_label=ctx.principal.label,
        actor_id=ctx.actor_id,
    )
    await _audit(ctx, "consent.form_created", "consent_form", form.id, template=payload.template_key)
    return await _form_out(ctx, form, full=True)


async def _form(ctx: TenantContext, form_id: str) -> ConsentForm:
    return await consent_service.get_form(
        ctx.session, ctx.tenant_id, parse_uuid(form_id, what="consent form")
    )


@router.get("/forms/{form_id}", response_model=ConsentFormOut)
async def get_form(
    form_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_READ)),
) -> ConsentFormOut:
    return await _form_out(ctx, await _form(ctx, form_id), full=True)


@router.get("/forms/{form_id}/versions/{number}", response_model=ConsentVersionOut)
async def get_version(
    form_id: str,
    number: int,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_READ)),
) -> ConsentVersionOut:
    form = await _form(ctx, form_id)
    counts = await consent_service.consent_counts(ctx.session, ctx.tenant_id, form.id)
    return _version_out(consent_service.version_numbered(form, number), counts, content=True)


@router.post("/forms/{form_id}/draft", response_model=ConsentFormOut)
async def start_draft(
    form_id: str,
    payload: DraftIn,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> ConsentFormOut:
    """Begin editing — or, with `from_version`, copy an older version into the draft."""
    form = await _form(ctx, form_id)
    had_draft = consent_service.version_with(form, consent_service.DRAFT) is not None
    draft = await consent_service.start_draft(
        ctx.session,
        form,
        from_version=payload.from_version,
        actor_label=ctx.principal.label,
    )
    if payload.from_version is not None:
        await _audit(
            ctx,
            "consent.version_restored",
            "consent_form",
            form.id,
            from_version=payload.from_version,
            draft=draft.number,
        )
    elif not had_draft:
        await _audit(ctx, "consent.draft_created", "consent_form", form.id, draft=draft.number)
    return await _form_out(ctx, form, full=True)


@router.put("/forms/{form_id}/draft", response_model=ConsentFormOut)
async def save_draft(
    form_id: str,
    payload: DraftContentIn,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> ConsentFormOut:
    form = await _form(ctx, form_id)
    draft = await consent_service.save_draft(
        ctx.session, form, payload.content, actor_label=ctx.principal.label
    )
    await _audit(ctx, "consent.draft_saved", "consent_form", form.id, draft=draft.number)
    return await _form_out(ctx, form, full=True)


@router.delete("/forms/{form_id}/draft", response_model=ConsentFormOut)
async def discard_draft(
    form_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> ConsentFormOut:
    form = await _form(ctx, form_id)
    await consent_service.discard_draft(ctx.session, form)
    await _audit(ctx, "consent.draft_discarded", "consent_form", form.id)
    return await _form_out(ctx, form, full=True)


@router.post("/forms/{form_id}/publish", response_model=ConsentFormOut)
async def publish(
    form_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> ConsentFormOut:
    form = await _form(ctx, form_id)
    previous = consent_service.version_with(form, consent_service.PUBLISHED)
    version = await consent_service.publish(ctx.session, form, actor_label=ctx.principal.label)
    if previous is not None:
        await _audit(ctx, "consent.version_archived", "consent_form", form.id, version=previous.number)
    await _audit(ctx, "consent.version_published", "consent_form", form.id, version=version.number)
    return await _form_out(ctx, form, full=True)


@router.post("/forms/{form_id}/reset", response_model=ConsentFormOut)
async def reset(
    form_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_MANAGE)),
) -> ConsentFormOut:
    """The draft becomes the system template again. Nothing published changes
    until the draft is published."""
    form = await _form(ctx, form_id)
    draft = await consent_service.reset_to_template(ctx.session, form, actor_label=ctx.principal.label)
    await _audit(ctx, "consent.reset_to_template", "consent_form", form.id, draft=draft.number)
    return await _form_out(ctx, form, full=True)


@router.get("/forms/{form_id}/versions/{number}/document.pdf")
async def version_document(
    form_id: str,
    number: int,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_READ)),
) -> Response:
    """One version as its printed document — the preview and the blank form for
    signing on paper are this same file. A draft prints marked as a draft."""
    form = await _form(ctx, form_id)
    version = consent_service.version_numbered(form, number)
    status = str(getattr(version.status, "value", version.status))
    content = build_consent_pdf(
        ConsentPrint(
            content=ConsentContent.model_validate(version.content),
            version_number=version.number,
            mode="draft" if status == "draft" else "blank",
        ),
        clinic=ctx.tenant.name,
        config=ctx.config,
        logo=await tenant_service.logo_for(ctx.session, ctx.tenant_id),
        tz=ctx.tz,
    )
    await _audit(ctx, "consent.form_printed", "consent_form", form.id, version=version.number)
    return _pdf(content, f"consent-form-v{version.number}.pdf")


# --- patients' consents -------------------------------------------------------------------


def _record_out(record: PatientConsent, form_name: str, *, content: bool = False) -> PatientConsentOut:
    c = ConsentContent.model_validate(record.content)
    return PatientConsentOut(
        id=str(record.id),
        patient_id=str(record.patient_id),
        form_id=str(record.form_id),
        form_name=form_name,
        title=c.title,
        version_number=record.version_number,
        status=consent_service.effective_status(record),
        requested_by=record.requested_by_label,
        requested_at=record.created_at,
        expires_at=record.expires_at,
        provider_id=str(record.provider_id) if record.provider_id else None,
        provider_name=record.provider_name,
        signer_name=record.signer_name,
        signer_relationship=record.signer_relationship,
        signed_at=record.signed_at,
        witnessed_by=record.witnessed_by_label,
        declined_at=record.declined_at,
        decline_reason=record.decline_reason,
        has_patient_signature=record.signed_at is not None,
        has_provider_signature=record.signed_at is not None and c.signatures.provider,
        acknowledged=list(record.acknowledged or []),
        field_values=dict(record.field_values or {}),
        patient=dict(record.patient_snapshot or {}),
        content=c if content else None,
    )


async def _names(ctx: TenantContext) -> dict:
    return {f.id: f.name for f in await consent_service.list_forms(ctx.session, ctx.tenant_id)}


@router.get("/records", response_model=list[PatientConsentOut])
async def list_records(
    patient_id: str | None = None,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_READ)),
) -> list[PatientConsentOut]:
    pid = parse_uuid(patient_id, what="patient") if patient_id else None
    names = await _names(ctx)
    return [
        _record_out(r, names.get(r.form_id, ""))
        for r in await consent_service.list_consents(ctx.session, ctx.tenant_id, patient_id=pid)
    ]


@router.post("/records", response_model=PatientConsentOut, status_code=201)
async def request_consent(
    payload: PatientConsentIn,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_REQUEST)),
) -> PatientConsentOut:
    """Ask a patient for consent on the form's published version — copied onto
    the record, with the patient's details, as they stand now."""
    patient = await patient_service.get_patient(
        ctx.session, ctx.tenant_id, parse_uuid(payload.patient_id, what="patient")
    )
    form = await _form(ctx, payload.form_id)
    record = await consent_service.request_consent(
        ctx.session,
        ctx.tenant,
        payload,
        patient=patient,
        form=form,
        actor=ctx.user,
        actor_label=ctx.principal.label,
    )
    await _audit(
        ctx,
        "consent.requested",
        "patient_consent",
        record.id,
        patient=str(patient.id),
        version=record.version_number,
    )
    return _record_out(record, form.name, content=True)


async def _record(ctx: TenantContext, consent_id: str, *, signatures: bool = False) -> PatientConsent:
    return await consent_service.get_consent(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(consent_id, what="consent"),
        signatures=signatures,
    )


@router.get("/records/{consent_id}", response_model=PatientConsentOut)
async def get_record(
    consent_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_READ)),
) -> PatientConsentOut:
    record = await _record(ctx, consent_id)
    return _record_out(record, (await _names(ctx)).get(record.form_id, ""), content=True)


@router.post("/records/{consent_id}/sign", response_model=PatientConsentOut)
async def sign(
    consent_id: str,
    payload: SignIn,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_REQUEST)),
) -> PatientConsentOut:
    """The patient signs on the clinic's device; the member of staff whose
    session it is stands as witness. Final."""
    record = await _record(ctx, consent_id, signatures=True)
    await consent_service.sign(ctx.session, record, payload, actor_label=ctx.principal.label)
    await _audit(
        ctx,
        "consent.signed",
        "patient_consent",
        record.id,
        patient=str(record.patient_id),
        version=record.version_number,
    )
    return _record_out(record, (await _names(ctx)).get(record.form_id, ""), content=True)


@router.post("/records/{consent_id}/decline", response_model=PatientConsentOut)
async def decline(
    consent_id: str,
    payload: DeclineIn,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_REQUEST)),
) -> PatientConsentOut:
    record = await _record(ctx, consent_id)
    await consent_service.decline(ctx.session, record, reason=payload.reason, actor_label=ctx.principal.label)
    await _audit(
        ctx,
        "consent.declined",
        "patient_consent",
        record.id,
        patient=str(record.patient_id),
        version=record.version_number,
    )
    return _record_out(record, (await _names(ctx)).get(record.form_id, ""), content=True)


@router.get("/records/{consent_id}/document.pdf")
async def record_document(
    consent_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CONSENT_READ)),
) -> Response:
    """The consent as the patient answered it — or, while pending, filled in
    and ready to sign on paper. Always the version they were asked."""
    record = await _record(ctx, consent_id, signatures=True)
    status = consent_service.effective_status(record)
    content = build_consent_pdf(
        ConsentPrint(
            content=ConsentContent.model_validate(record.content),
            version_number=record.version_number,
            mode=status,
            patient=dict(record.patient_snapshot or {}),
            field_values=dict(record.field_values or {}),
            acknowledged=list(record.acknowledged or []),
            provider_name=record.provider_name,
            requested_at=record.created_at,
            signer_name=record.signer_name,
            signer_relationship=record.signer_relationship,
            signed_at=record.signed_at,
            witnessed_by=record.witnessed_by_label,
            patient_signature=record.patient_signature,
            provider_signature=record.provider_signature,
            declined_at=record.declined_at,
            decline_reason=record.decline_reason,
        ),
        clinic=ctx.tenant.name,
        config=ctx.config,
        logo=await tenant_service.logo_for(ctx.session, ctx.tenant_id),
        tz=ctx.tz,
    )
    await _audit(ctx, "consent.printed", "patient_consent", record.id, version=record.version_number)
    mrn = (record.patient_snapshot or {}).get("mrn", "patient")
    return _pdf(content, f"consent-{mrn}-v{record.version_number}.pdf")
