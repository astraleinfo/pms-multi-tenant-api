"""The realtime half of §3.6.

One socket per signed-in screen, carrying nothing but "something in this
clinic's queue changed". Clients re-read the queue over the ordinary HTTP
endpoint when they get one, so there is exactly one description of what the
queue *is*, and the socket never becomes a second, divergent API.

The connection is authorised the same way every request is — the token names a
clinic, the address names a clinic, and they have to be the same one — and it
closes itself when the access token expires, so a socket cannot outlive the
session that opened it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session, host_context
from app.core.events import broker
from app.core.permissions import Permission, role_has
from app.core.security import TokenError, decode_access_token
from app.core.tenancy import HostContext
from app.db.session import set_tenant
from app.models.enums import AccountStatus, TenantStatus
from app.models.identity import User
from app.services import tenants as tenant_service

router = APIRouter(tags=["realtime"])

log = logging.getLogger("pms.ws")

# Close codes the client can act on.
WS_UNAUTHORISED = 4401
WS_FORBIDDEN = 4403
WS_TOKEN_EXPIRED = 4440

HEARTBEAT_SECONDS = 25


@router.websocket("/ws/queue")
async def queue_socket(
    websocket: WebSocket,
    access_token: str = Query(description="A current access token. Browsers cannot set headers on a socket."),
    session: AsyncSession = Depends(db_session),
    host: HostContext = Depends(host_context),
) -> None:
    try:
        claims = decode_access_token(access_token)
    except TokenError:
        await websocket.close(code=WS_UNAUTHORISED, reason="Sign in to continue.")
        return

    if claims.subject_kind != "user" or claims.tenant_id is None:
        await websocket.close(code=WS_UNAUTHORISED, reason="Not a clinic session.")
        return

    if host.kind != "tenant" or not host.slug or (claims.tenant_slug and claims.tenant_slug != host.slug):
        await websocket.close(code=WS_UNAUTHORISED, reason="Wrong address for this session.")
        return

    tenant = await tenant_service.tenant_by_slug(session, host.slug)
    if tenant is None or tenant.id != claims.tenant_id or tenant.status == TenantStatus.SUSPENDED:
        await websocket.close(code=WS_UNAUTHORISED, reason="Wrong address for this session.")
        return

    await set_tenant(session, tenant.id)
    user = await session.get(User, claims.subject_id)
    if user is None or user.tenant_id != tenant.id or user.status != AccountStatus.ACTIVE:
        await websocket.close(code=WS_UNAUTHORISED, reason="Sign in to continue.")
        return

    if not role_has(user.role, Permission.QUEUE_READ):
        await websocket.close(code=WS_FORBIDDEN, reason="Your role does not allow that.")
        return

    await websocket.accept()

    # The socket is no older than the token that opened it.
    expires_in = max(0.0, claims.expires_at.timestamp() - datetime.now(timezone.utc).timestamp())

    async with broker.subscribe(tenant.id) as events:
        await websocket.send_json(
            {
                "type": "hello",
                "tenant": str(tenant.id),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "expires_in": int(expires_in),
            }
        )

        reader = asyncio.create_task(_drain(websocket))
        deadline = asyncio.get_running_loop().time() + expires_in

        try:
            while True:
                if reader.done():
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await websocket.close(code=WS_TOKEN_EXPIRED, reason="Token expired.")
                    break
                try:
                    event = await asyncio.wait_for(
                        events.get(), timeout=min(HEARTBEAT_SECONDS, remaining)
                    )
                except asyncio.TimeoutError:
                    # Keeps proxies from reaping an idle clinic.
                    await websocket.send_json(
                        {"type": "ping", "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    )
                    continue
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader


async def _drain(websocket: WebSocket) -> None:
    """Read and discard whatever the client sends.

    The socket is one-directional by design — a client asking the server to *do*
    something goes through the HTTP API, where the guards are. This exists so a
    disconnect is noticed promptly.
    """
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await websocket.receive_text()


@router.get("/ws/health", include_in_schema=False)
async def socket_health() -> dict:
    return {"status": "ok", "transport": "websocket", "path": "/v1/ws/queue"}
