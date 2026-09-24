"""Console: onboard a clinic's staff and patients from a spreadsheet.

`GET  .../imports/{kind}/template` — the sample sheet for this clinic.
`POST .../imports/{kind}`          — check an upload; with `commit=true`,
                                     write it, but only if every row passed.

`kind` is `users` or `patients`. See `app/services/imports.py` for the rules.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, current_platform_user, db_session, parse_uuid, user_agent
from app.core.errors import NotFound, ValidationFailed
from app.db.session import set_tenant
from app.models.enums import FieldEntity
from app.models.identity import PlatformUser
from app.models.tenancy import Tenant
from app.schemas.clinical import PatientIn
from app.schemas.common import ApiModel, zone
from app.services import audit
from app.services import imports as import_service
from app.services import patients as patient_service
from app.services import tenants as tenant_service

log = logging.getLogger("pms.imports")

router = APIRouter(prefix="/platform/tenants/{tenant_id}/imports", tags=["platform"])

Kind = Literal["users", "patients"]


class ImportRowOut(ApiModel):
    row: int
    label: str
    ok: bool
    errors: list[str]


class ImportReportOut(ApiModel):
    kind: str
    total: int
    valid: int
    invalid: int
    committed: bool
    created: int = 0
    #: staff only: invitations that left. A failed send is not a failed import
    #: — the account exists and "resend" is on the clinic's staff list.
    invited: int = 0
    rows: list[ImportRowOut]


async def _clinic(session: AsyncSession, tenant_id: str) -> Tenant:
    clinic = await session.get(Tenant, parse_uuid(tenant_id, what="clinic"))
    if clinic is None:
        raise NotFound("No such clinic.")
    await set_tenant(session, clinic.id)
    return clinic


async def _columns(session: AsyncSession, clinic: Tenant, kind: Kind):
    if kind == "users":
        return import_service.USER_COLUMNS, []
    schema = await tenant_service.effective_schema(session, clinic, FieldEntity.PATIENT)
    return import_service.patient_columns(schema), schema


@router.get("/{kind}/template")
async def template(
    tenant_id: str,
    kind: Kind,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> Response:
    """The sample sheet, built for this clinic: its own custom patient fields
    are columns, with their dropdowns, and a second sheet says what each is."""
    clinic = await _clinic(session, tenant_id)
    columns, _ = await _columns(session, clinic, kind)
    content = import_service.build_template(kind, columns, clinic=clinic.name)
    name = f"{clinic.slug}-{'staff' if kind == 'users' else 'patients'}-import.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.post("/{kind}", response_model=ImportReportOut)
async def run_import(
    tenant_id: str,
    kind: Kind,
    request: Request,
    file: UploadFile = File(..., description="The filled-in sample sheet (.xlsx) or a .csv."),
    commit: bool = Query(default=False, description="Write the rows. Refused unless every row passes."),
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> ImportReportOut:
    clinic = await _clinic(session, tenant_id)
    columns, schema = await _columns(session, clinic, kind)
    content = await file.read(import_service.MAX_FILE_BYTES + 1)
    rows = import_service.read_rows(content, file.filename or "", columns, kind=kind)

    if kind == "users":
        results = await import_service.check_users(session, clinic, rows)
    else:
        results = await import_service.check_patients(session, clinic, columns, schema, rows)

    report = ImportReportOut(
        kind=kind,
        total=len(results),
        valid=sum(r.ok for r in results),
        invalid=sum(not r.ok for r in results),
        committed=False,
        rows=[ImportRowOut(row=r.row, label=r.label, ok=r.ok, errors=r.errors) for r in results],
    )
    if not commit:
        return report
    if report.invalid:
        raise ValidationFailed(
            f"{report.invalid} row(s) need fixing first — nothing was imported.",
            details={"rows": [r.model_dump() for r in report.rows if not r.ok]},
        )

    label = f"{staff.name} <{staff.email}>"
    if kind == "users":
        created, invited = await import_service.create_users(session, clinic, results, issued_by=label)
    else:
        created, invited = await _create_patients(session, clinic, results), 0

    await audit.record_for_tenant(
        session, clinic.id, action=f"platform.import_{kind}", actor_kind="platform",
        actor_id=staff.id, actor_label=label, entity="import", entity_id=file.filename or "",
        ip=client_ip(request), user_agent=user_agent(request),
        meta={"rows": report.total, "created": created, "invited": invited},
    )
    return report.model_copy(update={"committed": True, "created": created, "invited": invited})


async def _create_patients(session, clinic: Tenant, results) -> int:
    today = datetime.now(zone(clinic.timezone)).date()
    for r in results:
        await patient_service.create_patient(session, clinic, PatientIn(**r.data), today=today)
    return len(results)
