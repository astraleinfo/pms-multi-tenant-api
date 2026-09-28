"""A spreadsheet waiting to be imported, or the outcome of one.

The upload is answered as soon as the file is stored; the checking and the
writing happen in the Celery worker, off the request. The row is the job: the
file sits on it (or in the bucket, under `storage_key`) only until the worker
has finished with it, pass or fail, and what is left afterwards is the report.
A worker that dies mid-import leaves the row `running` with the file still in
place, and the redelivered job starts it again — the import is one
transaction, so a half-written one was never committed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import ImportJobStatus


class ImportJob(Base, TenantScoped, TimestampMixin):
    __tablename__ = "import_jobs"
    __extra_table_args__ = (
        # The sweep: still to run, oldest first.
        Index("ix_import_jobs_status_created", "status", "created_at"),
        Index("ix_import_jobs_tenant_kind_created", "tenant_id", "kind", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: what is being imported — `inventory` today
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ImportJobStatus] = mapped_column(
        Enum(ImportJobStatus, name="import_job_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ImportJobStatus.QUEUED,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: the upload itself, on the database backend; cleared when the job ends
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    #: the upload's key in the bucket, on the S3 backend; cleared when the job ends
    storage_key: Mapped[str | None] = mapped_column(String(300), nullable=True)

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    requested_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    #: what the audit trail records the import against — `platform` for a support session
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="user")

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: the one sentence a person reads when it failed
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: counts, and on a failure the rows that need fixing
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
