"""The Celery application.

    celery -A app.worker.celery_app worker --loglevel=INFO -Q pms
    celery -A app.worker.celery_app beat --loglevel=INFO

(`--pool=solo` on Windows, where the prefork pool does not work.)

A restart or a deploy must not lose a job. Three settings carry that, and the
database carries the rest:

*   `task_acks_late` — a job is acknowledged when it *finishes*, not when it is
    taken. A worker killed mid-job never acked it, so the broker hands it out
    again once the visibility timeout passes.
*   `task_reject_on_worker_lost` — if the child process dies (OOM, SIGKILL),
    the job goes straight back on the queue instead of being marked failed.
*   `worker_prefetch_multiplier = 1` — a worker holds only the job it is
    running, so a shutdown strands at most one, not a buffer of them.

A warm shutdown (SIGTERM, which is what `docker stop` and most deploys send)
lets the running job finish first; give the container a stop grace period
longer than one send. Redis runs with `--appendonly yes`, so the queue itself
survives a Redis restart.

And behind all of it, the row: every job's state lives in PostgreSQL, and beat
runs `sweep` every `CELERY_SWEEP_SECONDS` to re-queue anything still pending.
A broker wiped clean delays delivery by one sweep; it loses nothing.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import settings
from app.core.logging import configure_logging

celery_app = Celery("pms", broker=settings.broker_url, include=["app.worker.tasks"])

celery_app.conf.update(
    task_default_queue=settings.celery_queue,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # The database row is the result; nothing reads Celery's.
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": settings.celery_visibility_timeout_seconds},
    # Publishing from a request must fail fast if Redis is down: the row is
    # already written and the sweep will enqueue it, so a hung request buys
    # nothing.
    broker_connection_timeout=3,
    worker_hijack_root_logger=False,
    beat_schedule={
        "sweep-support-requests": {
            "task": "pms.support.sweep",
            "schedule": float(settings.celery_sweep_seconds),
        },
    },
)


@celery_app.on_after_configure.connect
def _logging(**_: object) -> None:
    configure_logging()
