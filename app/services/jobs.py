"""Reading what the workers are doing.

Flower is already subscribed to Celery's event stream and already keeps the
task history; this is a reader in front of it, not a second monitor. It asks
Flower over the container network, translates the answer into the vocabulary
the rest of the API speaks, and counts the six buckets the console shows.

Two things shape the whole module:

*   **Flower is never trusted to be up.** It is one container among several and
    it holds its history in memory, so a restart is an empty window, not an
    outage of the console. Every read here answers with `available` and a
    sentence rather than raising, and the page says which it got.
*   **Flower's window is not all of time.** `--max-tasks` bounds what it
    remembers, so the totals are "of the last N jobs", and `summary.window`
    says what N turned out to be. Counting inside it is honest; calling the
    result "failed jobs" without saying since when would not be.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from time import monotonic
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import set_tenant
from app.models.enums import CELERY_STATE_STATUS, JobStatus
from app.models.identity import User
from app.models.tenancy import Tenant
from app.schemas.jobs import JobArgumentOut, JobOut, JobsOut, JobSummaryOut, WorkerOut

log = logging.getLogger("pms.jobs")

#: One client for the process. Flower is a neighbour on the container network
#: and the console polls it, so a fresh TCP connection per tile is waste.
_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()

#: Whether the last read got through. The console polls every few seconds, so a
#: monitor that is down would otherwise write the same warning to the log
#: several times a minute for as long as it stays down — which buries whatever
#: else the log is trying to say. The first failure is a warning, the ones
#: behind it are debug, and coming back is a warning again so the recovery is
#: as visible as the outage.
_reachable = True

#: When the workers were last re-inspected. See `flower_inspect_seconds`.
_inspected_at = 0.0


def _note_unreachable(problem: BaseException) -> None:
    global _reachable
    log.log(
        logging.WARNING if _reachable else logging.DEBUG,
        "could not read the job monitor: %s", problem,
    )
    _reachable = False


def _note_recovered() -> None:
    global _reachable
    if not _reachable:
        log.warning("the job monitor is answering again")
    _reachable = True

UNAVAILABLE = "The job monitor is not reachable. Jobs are still running; this is only the view."
DISABLED = "Job monitoring is switched off for this deployment (FLOWER_ENABLED)."


async def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(settings.flower_timeout_seconds),
                    auth=settings.flower_credentials,
                    follow_redirects=False,
                )
    return _client


async def close() -> None:
    """Called from the application's lifespan, like the database engine."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """One GET against Flower, or `None` if it could not be read.

    Flower's own error text never reaches the caller: a 500 from it is an
    operational fact about this deployment, not something to forward.
    """
    http = await client()
    response = await http.get(f"{settings.flower_api_base}{path}", params=params)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------- translation


def _moment(value: Any) -> str | None:
    """A Celery event stamp — seconds since the epoch — as ISO-8601 UTC."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")


def _seconds(value: Any) -> float | None:
    return round(float(value), 3) if isinstance(value, (int, float)) and value >= 0 else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


# --- what a job is, in words ------------------------------------------------------------

#: Every task the platform has registered, and the two sweeps it used to run —
#: they stay in the monitor's history for a while after a deploy.
TASK_LABELS = {
    "pms.invitations.deliver": "Invitation or password reset email",
    "pms.support.deliver": "Support request email",
    "pms.imports.run": "Spreadsheet import",
    "pms.inventory.import": "Inventory import",
    "pms.rent.cycle": "Monthly billing cycle",
    "pms.support.sweep": "Support request sweep (retired)",
    "pms.imports.sweep": "Import sweep (retired)",
}

#: The positional order of each task's arguments, for a job sent before they
#: went by name.
_POSITIONAL = {
    "pms.invitations.deliver": ("tenant_id", "user_id", "purpose", "issued_by_label", "ip", "user_agent"),
    "pms.support.deliver": ("tenant_id", "request_id"),
    "pms.imports.run": ("tenant_id", "job_id"),
    "pms.inventory.import": ("tenant_id", "job_id"),
}

#: Argument names in the order the screen lists them, with their labels.
#: Anything not here is shown under its own name; the requester's address and
#: browser are never shown.
_ARGUMENT_LABELS = {
    "purpose": "Email",
    "tenant_id": "Clinic",
    "user_id": "Account",
    "request_id": "Support request",
    "job_id": "Import",
    "requested_by": "Requested by",
    "issued_by_label": "Requested by",
}
_HIDDEN = {"ip", "user_agent"}
_PURPOSES = {"invite": "Invitation", "reset": "Password reset", "reset_password": "Password reset"}


def _literal(text: str) -> Any:
    """A repr Celery put on the wire, read back. Never evaluated as code."""
    try:
        return ast.literal_eval(text) if text.strip() else None
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _call(job: JobOut) -> dict[str, Any]:
    """The job's arguments by name, whichever way it was sent."""
    call: dict[str, Any] = {}
    positional = _literal(job.args)
    if isinstance(positional, (tuple, list)):
        names = _POSITIONAL.get(job.name, ())
        for i, value in enumerate(positional):
            call[names[i] if i < len(names) else f"argument {i + 1}"] = value
    named = _literal(job.kwargs)
    if isinstance(named, dict):
        call.update({str(k): v for k, v in named.items()})
    return call


def _label(name: str, call: dict[str, Any]) -> str:
    if name == "pms.invitations.deliver" and call.get("purpose") in _PURPOSES:
        return f"{_PURPOSES[call['purpose']]} email"
    return TASK_LABELS.get(name, name)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


class _Names:
    """Clinics and accounts by id, for the jobs on one page."""

    def __init__(self) -> None:
        self.clinics: dict[uuid.UUID, str] = {}
        self.accounts: dict[uuid.UUID, str] = {}

    @classmethod
    async def read(cls, session: AsyncSession | None, calls: list[dict[str, Any]]) -> _Names:
        names = cls()
        if session is None:
            return names
        clinic_ids = {t for c in calls if (t := _uuid(c.get("tenant_id")))}
        if clinic_ids:
            rows = await session.execute(select(Tenant.id, Tenant.name).where(Tenant.id.in_(clinic_ids)))
            names.clinics = {i: n for i, n in rows}
        # An account is the clinic's row, so it is read with that clinic bound —
        # the console gets no way around row-level security here either.
        wanted: dict[uuid.UUID, set[uuid.UUID]] = {}
        for c in calls:
            tenant, account = _uuid(c.get("tenant_id")), _uuid(c.get("user_id"))
            if tenant in names.clinics and account:
                wanted.setdefault(tenant, set()).add(account)
        for tenant, accounts in wanted.items():
            await set_tenant(session, tenant)
            rows = await session.execute(select(User.id, User.name, User.email).where(User.id.in_(accounts)))
            names.accounts.update({i: f"{n} <{e}>" for i, n, e in rows})
        return names

    def value(self, key: str, value: Any) -> str:
        found = _uuid(value)
        if key == "tenant_id" and found:
            return self.clinics.get(found, str(value))
        if key == "user_id" and found:
            return self.accounts.get(found, str(value))
        if key == "purpose":
            return _PURPOSES.get(str(value), str(value))
        return str(value)


def _arguments(call: dict[str, Any], names: _Names) -> list[JobArgumentOut]:
    order = list(_ARGUMENT_LABELS)
    shown: list[JobArgumentOut] = []
    for key in sorted(call, key=lambda k: order.index(k) if k in order else len(order)):
        value = call[key]
        if key in _HIDDEN or value in ("", None):
            continue
        label = _ARGUMENT_LABELS.get(key, key.replace("_", " ").capitalize())
        if any(a.label == label for a in shown):
            continue  # `requested_by` and `issued_by_label` are the same fact
        shown.append(JobArgumentOut(label=label, value=names.value(key, value)))
    return shown


async def _describe(session: AsyncSession | None, jobs: list[JobOut]) -> None:
    """Fill in each job's arguments, clinics and accounts by name."""
    calls = [_call(job) for job in jobs]
    names = await _Names.read(session, calls)
    for job, call in zip(jobs, calls, strict=True):
        job.arguments = _arguments(call, names)


def _job(task_id: str, raw: dict[str, Any], *, detail: bool = False) -> JobOut:
    state = _text(raw.get("state")) or "PENDING"
    status = CELERY_STATE_STATUS.get(state, JobStatus.PENDING)

    started = raw.get("started")
    finished = raw.get("succeeded") or raw.get("failed") or raw.get("revoked")
    runtime = _seconds(raw.get("runtime"))
    running = status is JobStatus.ACTIVE and not finished

    if running and runtime is None and isinstance(started, (int, float)):
        # A job still in flight has no runtime of its own yet — it has an age,
        # which is the number the "how long has this been going" column wants.
        runtime = round(datetime.now(tz=timezone.utc).timestamp() - float(started), 3)

    name = _text(raw.get("name")) or "unknown"
    args, kwargs = _text(raw.get("args")), _text(raw.get("kwargs"))
    job = JobOut(
        id=task_id,
        name=name,
        status=status,
        state=state,
        worker=_text(raw.get("worker")) or None,
        queue=_text(raw.get("routing_key")) or None,
        args=args,
        kwargs=kwargs,
        received_at=_moment(raw.get("received")) or _moment(raw.get("sent")),
        started_at=_moment(started),
        finished_at=_moment(finished),
        runtime_seconds=runtime,
        running=running,
        retries=int(raw.get("retries") or 0),
        exception=_text(raw.get("exception")).strip() or None,
        traceback=(_text(raw.get("traceback")).strip() or None) if detail else None,
        result=(_text(raw.get("result")).strip() or None) if detail else None,
        eta=_moment(raw.get("eta")) or (_text(raw.get("eta")) or None),
    )
    job.label = _label(name, _call(job))
    return job


def _due_an_inspection() -> bool:
    """Whether this read should make Flower go and ask the workers again.

    Flower inspects once, at startup, and then only when a request carries
    `refresh=1`. A monitor that came up before its workers did — which is what
    happens every time the container set starts — has an empty answer cached
    and would keep serving it forever. So the read asks for a refresh now and
    then: often enough that a new worker appears within half a minute, rarely
    enough that a screen polling every few seconds is not pinging every worker
    on the platform each time.
    """
    global _inspected_at
    now = monotonic()
    if now - _inspected_at < settings.flower_inspect_seconds:
        return False
    _inspected_at = now
    return True


def _sort_key(job: JobOut) -> str:
    """Newest first, by whatever the most recent thing to happen to it was."""
    return job.finished_at or job.started_at or job.received_at or ""


def _tasks(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """Flower has answered `/api/tasks` as both a map and a list of pairs."""
    if isinstance(payload, dict):
        return [(str(k), v) for k, v in payload.items() if isinstance(v, dict)]
    if isinstance(payload, list):
        out = []
        for entry in payload:
            if isinstance(entry, (list, tuple)) and len(entry) == 2 and isinstance(entry[1], dict):
                out.append((str(entry[0]), entry[1]))
            elif isinstance(entry, dict) and entry.get("uuid"):
                out.append((str(entry["uuid"]), entry))
        return out
    return []


def _counter(row: dict[str, Any], name: str) -> int:
    """One of a worker's running totals.

    Flower builds these from Celery's event names, so they arrive as
    `task-succeeded` rather than `succeeded`. Both spellings are read: the
    hyphenated one is what 2.x sends, and a bare one costs nothing to accept.
    """
    value = row.get(f"task-{name}", row.get(name))
    return int(value or 0)


def _workers(dashboard: Any, detail: Any) -> list[WorkerOut]:
    """The worker list, from Flower's worker view, widened by `/api/workers`.

    The worker view knows which workers are answering and what each has got
    through; `/api/workers` knows how many jobs each will run at once and which
    queues it is on. The second is the one that may be missing — a worker that
    has not been inspected yet simply has no concurrency to report, and the row
    still renders.
    """
    rows = dashboard.get("data") if isinstance(dashboard, dict) else dashboard
    if not isinstance(rows, list):
        return []

    extra = detail if isinstance(detail, dict) else {}
    workers: list[WorkerOut] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("hostname"))
        if not name:
            continue
        stats = extra.get(name) if isinstance(extra.get(name), dict) else {}
        pool = (stats.get("stats") or {}).get("pool") if isinstance(stats, dict) else {}
        queues = (stats.get("active_queues") or []) if isinstance(stats, dict) else []
        load = row.get("loadavg")
        workers.append(
            WorkerOut(
                name=name,
                online=bool(row.get("status")),
                active=int(row.get("active") or 0),
                processed=int(row.get("processed") or 0),
                succeeded=_counter(row, "succeeded"),
                failed=_counter(row, "failed"),
                retried=_counter(row, "retried"),
                concurrency=(
                    int(pool.get("max-concurrency"))
                    if isinstance(pool, dict) and pool.get("max-concurrency") is not None
                    else None
                ),
                queues=sorted(
                    {_text(q.get("name")) for q in queues if isinstance(q, dict) and q.get("name")}
                ),
                load_average=[float(x) for x in load] if isinstance(load, list) else [],
            )
        )
    return sorted(workers, key=lambda w: (not w.online, w.name))


def _summary(jobs: list[JobOut], workers: list[WorkerOut]) -> JobSummaryOut:
    counts: dict[JobStatus, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    return JobSummaryOut(
        active=counts.get(JobStatus.ACTIVE, 0),
        pending=counts.get(JobStatus.PENDING, 0),
        completed=counts.get(JobStatus.COMPLETED, 0),
        failed=counts.get(JobStatus.FAILED, 0),
        retried=counts.get(JobStatus.RETRIED, 0),
        revoked=counts.get(JobStatus.REVOKED, 0),
        workers_online=sum(1 for w in workers if w.online),
        workers_total=len(workers),
        window=len(jobs),
    )


# --------------------------------------------------------------------- filters


def _matches(
    job: JobOut,
    *,
    status: JobStatus | None,
    task: str | None,
    worker: str | None,
    since: datetime | None,
    until: datetime | None,
    search: str,
) -> bool:
    if status is not None and job.status != status:
        return False
    if task and job.name != task:
        return False
    if worker and job.worker != worker:
        return False
    if since or until:
        at = _sort_key(job)
        if not at:
            # Nothing has happened to it yet that carries a time. A window
            # asked for by date cannot contain it, so it is left out.
            return False
        moment = datetime.fromisoformat(at)
        if since and moment < since:
            return False
        if until and moment > until:
            return False
    if search:
        needle = search.casefold()
        haystack = " ".join(
            filter(None, [job.label, job.name, job.id, job.args, job.kwargs, job.worker, job.exception])
        )
        if needle not in haystack.casefold():
            return False
    return True


def _aware(value: datetime | None) -> datetime | None:
    """A filter bound the browser sent without a zone is read as UTC, because
    that is the clock every stamp on this screen is printed in."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------- the reads


async def overview(
    *,
    status: JobStatus | None = None,
    task: str | None = None,
    worker: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str = "",
    limit: int = 25,
    offset: int = 0,
    session: AsyncSession | None = None,
) -> JobsOut:
    """One read of the monitor: the tiles, the filter options, and a page.

    The counts and the dropdown options are taken over the whole window and
    the filters are applied after, so narrowing to "failed on worker-2" does
    not quietly rewrite the totals above it.
    """
    if not settings.flower_enabled:
        return JobsOut(available=False, problem=DISABLED, limit=limit, offset=offset)

    # The history is what the screen is for; the other two only widen it. A
    # `/api/workers` that times out costs a concurrency column, not the page.
    tasks, dashboard, detail = await asyncio.gather(
        _get("/api/tasks", {"limit": settings.flower_task_window}),
        # `/workers`, not `/dashboard`: 2.x renders the worker list there, and
        # `json=1` is what turns it from a page into an answer.
        _get("/workers", {"json": 1}),
        _get("/api/workers", {"refresh": 1} if _due_an_inspection() else None),
        return_exceptions=True,
    )
    if isinstance(tasks, BaseException):
        _note_unreachable(tasks)
        return JobsOut(available=False, problem=UNAVAILABLE, limit=limit, offset=offset)
    _note_recovered()
    if isinstance(dashboard, BaseException):
        log.debug("could not read the worker list: %s", dashboard)
        dashboard = None
    if isinstance(detail, BaseException):
        detail = None

    jobs = sorted((_job(i, raw) for i, raw in _tasks(tasks)), key=_sort_key, reverse=True)
    workers = _workers(dashboard, detail)

    matched = [
        job
        for job in jobs
        if _matches(
            job,
            status=status,
            task=task,
            worker=worker,
            since=_aware(since),
            until=_aware(until),
            search=search.strip(),
        )
    ]
    page = matched[offset : offset + limit] if limit else matched
    await _describe(session, page)

    return JobsOut(
        available=True,
        summary=_summary(jobs, workers),
        workers=workers,
        task_names=sorted({job.name for job in jobs}),
        task_labels={job.name: TASK_LABELS.get(job.name, job.name) for job in jobs},
        # Every worker the window mentions, including one that has since gone
        # away — its jobs are still in the history and still worth filtering to.
        worker_names=sorted({job.worker for job in jobs if job.worker} | {w.name for w in workers}),
        items=page,
        total=len(matched),
        limit=limit,
        offset=offset,
    )


async def job(task_id: str, session: AsyncSession | None = None) -> JobOut | None:
    """One job in full — the traceback and the return value included."""
    if not settings.flower_enabled:
        return None
    try:
        raw = await _get(f"/api/task/info/{task_id}")
    except (httpx.HTTPError, ValueError) as exc:
        # Not throttled and not a verdict on the monitor: this read happens
        # when somebody opens one job, and "no such job" arrives as a 404 from
        # Flower, which is an answer rather than a silence.
        log.info("could not read job %s: %s", task_id, exc)
        return None
    if not isinstance(raw, dict):
        return None
    found = _job(_text(raw.get("uuid")) or task_id, raw, detail=True)
    await _describe(session, [found])
    return found
