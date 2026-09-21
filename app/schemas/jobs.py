"""Background-job payloads — what the console's jobs section reads.

Times here are ISO-8601 in UTC, not the `YYYY-MM-DD HH:mm` the clinical screens
use. A job is a machine event on the platform's own hardware: it belongs to no
clinic, so it has no local timezone to be rendered in, and a second matters
when half of what is being read is a duration.

Nothing here is a database row. Flower holds the task history in memory and
this is its vocabulary translated into ours, which is why every payload carries
`available` and `window` — how much of the past Flower can still see is a fact
about the monitor, not about the platform, and a screen that hid it would read
`0 failed` the same whether nothing failed or nothing was watching.
"""

from __future__ import annotations

from app.models.enums import JobStatus
from app.schemas.common import ApiModel


class JobOut(ApiModel):
    id: str
    #: the registered task name, e.g. `pms.support.deliver`
    name: str
    #: the bucket the console counts this in
    status: JobStatus
    #: Celery's own state, kept because the six buckets fold some of them
    #: together and an operator reading a single job wants the real word
    state: str
    worker: str | None = None
    queue: str | None = None
    #: Celery puts the call on the wire already rendered, so these are the
    #: reprs the worker was handed rather than structured values.
    args: str = ""
    kwargs: str = ""
    received_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    #: How long it ran. Flower reports it for a job that finished; for one
    #: still running this is how long it has been going so far.
    runtime_seconds: float | None = None
    #: True while `runtime_seconds` is still climbing.
    running: bool = False
    retries: int = 0
    #: One line, on a job that failed — the exception as the worker raised it.
    exception: str | None = None
    #: The whole traceback, and whatever the job returned. Only the single-job
    #: read carries these; a list of five hundred would be mostly stack.
    traceback: str | None = None
    result: str | None = None
    eta: str | None = None


class WorkerOut(ApiModel):
    name: str
    #: whether the worker answered its last heartbeat
    online: bool
    #: jobs it is running right now
    active: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    #: how many jobs it will run at once, when the worker reports it
    concurrency: int | None = None
    queues: list[str] = []
    load_average: list[float] = []


class JobSummaryOut(ApiModel):
    """The tiles at the top of the page, counted before any filter is applied.

    Same rule as the clinical lists' `counts`: they are taken before the status
    filter, so "Failed 3" stays true while the Active tab is the one on screen.
    """

    active: int = 0
    pending: int = 0
    completed: int = 0
    failed: int = 0
    retried: int = 0
    revoked: int = 0
    workers_online: int = 0
    workers_total: int = 0
    #: how many task records these counts were taken over
    window: int = 0


class JobsOut(ApiModel):
    """One read of the monitor: the tiles, the filters' options, and a page.

    `available` false is not an error — the jobs section renders perfectly well
    while Flower is restarting, and says so, rather than showing a page of
    zeroes that reads like a quiet platform.
    """

    available: bool
    problem: str | None = None
    summary: JobSummaryOut = JobSummaryOut()
    workers: list[WorkerOut] = []
    #: every task name and worker seen in the window — the filter dropdowns
    task_names: list[str] = []
    worker_names: list[str] = []
    items: list[JobOut] = []
    total: int = 0
    limit: int = 0
    offset: int = 0


class FlowerSessionOut(ApiModel):
    """What the console gets when it asks to open Flower's own pages.

    The token itself is not here — it is set as an HttpOnly cookie, exactly as
    the refresh token is, so script in the page cannot read it and hand it on.
    """

    url: str
    expires_in: int
