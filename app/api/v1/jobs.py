"""Background jobs, in the super-admin console.

Three things live here, and they are three because they answer three different
questions:

*   `GET /v1/platform/jobs` — the console's own screen. Flower's numbers in
    this API's vocabulary, counted, filtered and paged like every other list
    the product serves.
*   `GET /v1/platform/jobs/tasks/{id}` — one job in full, traceback included.
*   `/v1/platform/jobs/flower/…` — Flower's own interface, reverse-proxied.
    For the questions the screen above does not answer. Flower is not
    published to a host port and never has been; this route is the only way to
    it from outside the container network, and it is behind the same check.

The check is `current_super_admin`, and the shape of its refusals is the point
of the feature: a clinic token — a practice admin's, a doctor's, a
receptionist's, a pharmacist's — is a perfectly valid session and is refused
with 403, not 401, because the answer is "not you", not "sign in again". A
platform account that is not a super admin gets the same. Hiding the menu item
is the front end's courtesy; this is what actually stops the request.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    bearer,
    current_super_admin,
    db_session,
    host_context,
    resolve_super_admin,
)
from app.core.config import settings
from app.core.errors import AuthError, NotFound
from app.core.security import create_access_token
from app.core.tenancy import HostContext
from app.models.enums import JobStatus
from app.models.identity import PlatformUser
from app.schemas.jobs import FlowerSessionOut, JobOut, JobsOut
from app.services import jobs as jobs_service

log = logging.getLogger("pms.jobs")

router = APIRouter(prefix="/platform/jobs", tags=["platform", "jobs"])

#: The cookie that lets a browser load Flower's own pages, and the claim that
#: makes it safe to set. A token carrying this scope opens the proxy below and
#: nothing else in the API — `current_tenant` and `current_platform_user` both
#: refuse any token that carries a scope at all.
FLOWER_COOKIE = "pms_flower"  # noqa: S105 - a cookie name, not a secret
FLOWER_SCOPE = "jobs:flower"

#: Flower *is* a document, so the API's `default-src 'none'` would blank it.
#: This is the `/docs` relaxation again and for the same reason, scoped to the
#: proxied responses: Flower ships its own assets, so `'self'` is the whole of
#: it, and `frame-ancestors 'self'` is what lets the console embed it.
FLOWER_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)

#: Hop-by-hop headers belong to one connection and must not be relayed.
HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "content-encoding",
        "content-length",
    }
)

#: What the browser is allowed to say to Flower. Everything else — its
#: Authorization, its PMS cookies, its Host — is this API's business and is
#: replaced or dropped on the way through.
FORWARDED_REQUEST_HEADERS = frozenset(
    {"accept", "accept-language", "content-type", "if-none-match", "if-modified-since"}
)

#: Dropped on the way back. The hop-by-hop ones because they describe one
#: connection; the CSP because this route writes its own; `www-authenticate`
#: because Flower's own basic auth is between it and this API — a browser
#: must never be asked for those credentials.
DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-security-policy", "www-authenticate"}

PROXIED_METHODS = ["GET", "HEAD", "POST"]


async def flower_caller(
    request: Request,
    session: AsyncSession = Depends(db_session, scope="function"),
    host: HostContext = Depends(host_context),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> PlatformUser:
    """Who is allowed through to Flower itself.

    Same person as everywhere else in this file, reached one extra way: a
    browser loading an interface cannot put an `Authorization` header on the
    stylesheet, the script and the dozen polls that page fires, so the console
    asks for a cookie first and the sub-resources carry that instead. The
    cookie is scoped, short-lived and HttpOnly; what it is not is a second
    standard of proof.

    How the token arrived decides what it is allowed to be. A bearer token is
    the console calling, and is held to everything `current_super_admin` holds
    it to — including the address, so a scoped cookie value copied into an
    `Authorization` header from a clinic's address gains nothing. The cookie is
    the browser fetching an asset, and it is the one case where the address is
    not re-checked: see `resolve_super_admin`.
    """
    if credentials and credentials.scheme.lower() == "bearer" and credentials.credentials:
        return await resolve_super_admin(request, session, host, credentials.credentials)

    scoped = request.cookies.get(FLOWER_COOKIE)
    if scoped:
        return await resolve_super_admin(
            request, session, host, scoped,
            allowed_scopes=(FLOWER_SCOPE,), check_host=False,
        )

    session_cookie = request.cookies.get("pms_access")
    if session_cookie:
        return await resolve_super_admin(request, session, host, session_cookie)

    raise AuthError("Sign in to continue.")


@router.get("", response_model=JobsOut)
async def read_jobs(
    status: JobStatus | None = Query(default=None, description="one of the six buckets"),
    task: str | None = Query(default=None, max_length=200, description="exact task name"),
    worker: str | None = Query(default=None, max_length=200, description="exact worker name"),
    since: datetime | None = Query(default=None, description="ISO-8601; naive is read as UTC"),
    until: datetime | None = Query(default=None),
    search: str = Query(default="", max_length=200),
    limit: int = Query(default=25, ge=0, le=200),
    offset: int = Query(default=0, ge=0),
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> JobsOut:
    return await jobs_service.overview(
        status=status, task=task, worker=worker, since=since, until=until,
        search=search, limit=limit, offset=offset, session=session,
    )


@router.get("/tasks/{task_id}", response_model=JobOut)
async def read_job(
    task_id: str,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> JobOut:
    found = await jobs_service.job(task_id, session)
    if found is None:
        raise NotFound("No such job — the monitor's history may have rolled past it.")
    return found


@router.post("/flower-session", response_model=FlowerSessionOut)
async def open_flower_session(
    response: Response,
    staff: PlatformUser = Depends(current_super_admin),
) -> FlowerSessionOut:
    """Let this browser load Flower's own pages for a while.

    The token minted here names the same person and carries a scope, so it
    opens the proxy and nothing else: presented to `/v1/platform/tenants` it
    is refused like any other token that is not a session. It is set as an
    HttpOnly cookie rather than returned, for the same reason the refresh
    token is — script in the page has no use for it and no business holding it.
    """
    token = create_access_token(
        subject_id=staff.id,
        subject_kind="platform",
        session_id=uuid.uuid4(),
        scope=FLOWER_SCOPE,
        ttl_seconds=settings.flower_session_ttl_seconds,
    )
    # Host-only, deliberately: no `domain`, whatever COOKIE_DOMAIN says. A
    # cookie scoped to `.astrale.health` would be sent from every clinic's
    # subdomain too, and this is the one credential in the product whose
    # address is not re-checked on use. Left host-only, the browser returns it
    # to the console and to nowhere else, which is the same guarantee by a
    # different route.
    response.set_cookie(
        FLOWER_COOKIE,
        token,
        max_age=settings.flower_session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=settings.flower_cookie_path,
    )
    return FlowerSessionOut(
        url=f"/{settings.flower_url_prefix.strip('/')}/",
        expires_in=settings.flower_session_ttl_seconds,
    )


@router.delete("/flower-session", status_code=204)
async def close_flower_session(
    response: Response,
    staff: PlatformUser = Depends(current_super_admin),
) -> None:
    """Hand the cookie back. Signing out of the console should not leave a
    browser holding a key to Flower for the rest of its lifetime."""
    response.delete_cookie(FLOWER_COOKIE, path=settings.flower_cookie_path)


@router.api_route("/flower", methods=PROXIED_METHODS, include_in_schema=False)
@router.api_route("/flower/{path:path}", methods=PROXIED_METHODS, include_in_schema=False)
async def flower_proxy(
    request: Request,
    path: str = "",
    staff: PlatformUser = Depends(flower_caller),
) -> Response:
    """Flower's own interface, relayed.

    Port 5555 is not published anywhere. Flower answers on the container
    network, this is the only route to it, and `flower_caller` above has
    already established that the person asking is a super admin — so a URL
    guessed or shared out of the console is refused exactly as the console's
    own reads are.

    What does not survive the relay: WebSockets, which Flower uses for its
    live task feed. Its pages fall back to polling, and the console's own
    screen — which does poll — is the one built for watching.
    """
    if not settings.flower_enabled:
        raise NotFound("Job monitoring is switched off for this deployment.")
    if ".." in path.split("/"):
        # Flower is the whole of what lives behind this, so a traversal buys
        # nothing — but a proxy that normalises its own input is one fewer
        # thing to reason about the day something else moves in beside it.
        raise NotFound("No such page.")

    prefix = settings.flower_url_prefix.strip("/")
    tail = "/".join(part for part in (prefix, path.strip("/")) if part)
    upstream = f"{settings.flower_url.rstrip('/')}/{tail}"
    # A directory URL keeps its trailing slash, or Flower answers with a
    # redirect built from its own address rather than from this proxy's.
    if (not path or path.endswith("/")) and not upstream.endswith("/"):
        upstream += "/"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in FORWARDED_REQUEST_HEADERS
    }
    body = await request.body() if request.method == "POST" else None

    http = await jobs_service.client()
    try:
        answer = await http.request(
            request.method,
            upstream,
            params=request.url.query or None,
            headers=headers,
            content=body,
        )
    except httpx.HTTPError as exc:
        log.warning("the job monitor did not answer %s: %s", path or "/", exc)
        return Response(
            content=b"The job monitor is not reachable.",
            status_code=502,
            media_type="text/plain",
            headers={"Content-Security-Policy": FLOWER_CSP},
        )

    out = {
        key: value
        for key, value in answer.headers.items()
        if key.lower() not in DROPPED_RESPONSE_HEADERS
    }
    # Set before the security middleware runs, which only fills in what is
    # missing — so these stand and the API-wide `default-src 'none'` does not.
    out["Content-Security-Policy"] = FLOWER_CSP
    out["X-Frame-Options"] = "SAMEORIGIN"
    return Response(content=answer.content, status_code=answer.status_code, headers=out)
