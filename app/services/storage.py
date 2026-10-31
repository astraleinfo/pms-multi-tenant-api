"""Where uploaded files live.

Two backends, chosen by `STORAGE_BACKEND`:

*   ``database`` — the bytes sit in the row itself. Row-level security covers
    them exactly as it covers the order they hang off. The default, and what
    development and the test suite use.
*   ``s3`` — the bytes go to a bucket, and the row keeps the key alongside the
    name, type and size. The database stays small and the files are served
    from storage built for them.

Whichever is configured, a *read* follows the row: a row with a `storage_key`
is fetched from S3, a row with `data` is answered from the database. So moving
a deployment from one backend to the other never strands a file uploaded
under the previous one.

Tenant isolation does not move into the bucket. Every key is built from the
tenant, the order and the attachment's own id — never from anything the client
sent — and an object is only ever reached through its row, which is found
through the tenant-bound session. There is no route that takes a key, and the
bucket should grant nothing to anyone but this application.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import aws
from app.core.config import settings
from app.core.errors import AppError

log = logging.getLogger("pms.storage")


class StorageUnavailable(AppError):
    """The file store could not be reached or refused the operation.

    A 503 rather than a 500: the request was sound and the application is not
    at fault, so the client is told to try again rather than that it failed.
    """

    status_code = 503
    code = "storage_unavailable"


def uses_s3() -> bool:
    return settings.storage_backend == "s3"


def object_key(*, tenant_id: uuid.UUID, order_id: uuid.UUID, attachment_id: uuid.UUID) -> str:
    """Where one attachment lives in the bucket.

    Grouped by tenant first, so a clinic's files can be listed, exported or
    erased under one prefix. Every part is a UUID the server minted, so there
    is no name here for a client to steer.
    """
    prefix = settings.s3_prefix.strip("/")
    path = f"tenants/{tenant_id}/lab-orders/{order_id}/attachments/{attachment_id}"
    return f"{prefix}/{path}" if prefix else path


def logo_object_key(*, tenant_id: uuid.UUID) -> str:
    """Where one clinic's logo lives in the bucket.

    A clinic has at most one, so this is a fixed path rather than a minted
    id: each upload overwrites the object in place, and nothing is left
    behind to clean up when it is replaced.
    """
    prefix = settings.s3_prefix.strip("/")
    path = f"tenants/{tenant_id}/logo"
    return f"{prefix}/{path}" if prefix else path


def platform_logo_object_key() -> str:
    """Where the platform's own logo lives in the bucket — one fixed path,
    outside every clinic's prefix, overwritten in place like a clinic's."""
    prefix = settings.s3_prefix.strip("/")
    path = "platform/logo"
    return f"{prefix}/{path}" if prefix else path


def _s3() -> Any:
    if not settings.s3_bucket:
        raise StorageUnavailable("File storage is not configured (S3_BUCKET is empty).")
    try:
        return aws.client(
            "s3",
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
        )
    except aws.AwsUnavailable as exc:
        raise StorageUnavailable(str(exc)) from exc


def _errors():
    """The exception types a boto3 call raises, imported only when used."""
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def _put_blocking(key: str, data: bytes, content_type: str) -> None:
    extra: dict[str, str] = {}
    if settings.s3_server_side_encryption:
        extra["ServerSideEncryption"] = settings.s3_server_side_encryption
        if settings.s3_server_side_encryption == "aws:kms" and settings.s3_kms_key_id:
            extra["SSEKMSKeyId"] = settings.s3_kms_key_id
    try:
        _s3().put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            # The download route sets its own disposition; nothing reads the
            # object directly, so this is only what a console user would see.
            ContentDisposition="attachment",
            **extra,
        )
    except _errors() as exc:
        log.error("could not store %s: %s", key, exc)
        raise StorageUnavailable("The file could not be stored. Try again.") from exc


def _get_blocking(key: str) -> bytes:
    try:
        response = _s3().get_object(Bucket=settings.s3_bucket, Key=key)
        return response["Body"].read()
    except _errors() as exc:
        log.error("could not read %s: %s", key, exc)
        raise StorageUnavailable("The file could not be read from storage. Try again.") from exc


def _delete_blocking(key: str) -> None:
    try:
        _s3().delete_object(Bucket=settings.s3_bucket, Key=key)
    except (StorageUnavailable, *_errors()) as exc:
        # Nothing to tell the client — the row is already gone. An object left
        # behind is unreachable, and costs storage rather than privacy.
        log.error("could not delete %s; it is orphaned: %s", key, exc)


async def put(key: str, data: bytes, content_type: str) -> None:
    await asyncio.to_thread(_put_blocking, key, data, content_type)


async def get(key: str) -> bytes:
    return await asyncio.to_thread(_get_blocking, key)


async def delete(key: str) -> None:
    await asyncio.to_thread(_delete_blocking, key)


#: Deletes scheduled after a commit, held so they are not collected mid-flight.
_pending: set[asyncio.Task] = set()


def delete_after_commit(session: AsyncSession, key: str) -> None:
    """Remove an object once the transaction that dropped its row commits.

    Never before: deleting the object first and then failing to commit would
    leave a row pointing at nothing — a report the lab believes is filed and
    nobody can open. The other way round, a commit followed by a failed delete,
    leaves an object nothing points at, which is only storage.
    """

    def _after_commit(_session) -> None:
        task = asyncio.get_running_loop().create_task(delete(key))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    event.listen(session.sync_session, "after_commit", _after_commit, once=True)


async def drain() -> None:
    """Wait for scheduled deletes — for tests, and for a clean shutdown."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
