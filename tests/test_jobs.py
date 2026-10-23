"""The background-jobs section of the console (§3.1).

Flower is not running here, and deliberately so. Whether the monitor answers
is a fact about a container; who is allowed to ask it is the feature, and the
two must not be entangled — a check that only holds while Flower happens to be
up is not a check. So the guard tests below assert on the status code and the
shaping tests run against captured Flower payloads.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.services import jobs as jobs_service
from tests.conftest import CONSOLE, NORTHGATE, sign_in

JOBS = "/v1/platform/jobs"
FLOWER = "/v1/platform/jobs/flower/"

#: Every jobs route, so a new one cannot be added without an opinion about it.
GUARDED = [
    ("GET", JOBS),
    ("GET", "/v1/platform/jobs/tasks/0c6d2b1a"),
    ("POST", "/v1/platform/jobs/flower-session"),
    ("GET", FLOWER),
    ("GET", "/v1/platform/jobs/flower/dashboard"),
]


async def _call(client, method: str, url: str, **kw):
    return await getattr(client, method.lower())(url, **kw)


# --- who may look ------------------------------------------------------------


@pytest.mark.parametrize(("method", "url"), GUARDED)
async def test_nobody_signed_in_is_asked_to_sign_in(client, method, url):
    response = await client.request(method, url, headers={"Host": CONSOLE})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "account",
    ["northgate_admin", "northgate_doctor", "northgate_reception",
     "northgate_pharmacist", "northgate_lab"],
)
@pytest.mark.parametrize(("method", "url"), GUARDED)
async def test_every_clinic_role_is_forbidden(client, account, method, url):
    """A practice admin holds the widest role inside a clinic, and it is still
    a clinic role. 403, not 401 — the session is fine, the person is not the
    platform."""
    signed = await sign_in(client, account)
    response = await _call(signed, method, url)
    assert response.status_code == 403, f"{account} {method} {url}"
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(("method", "url"), GUARDED)
async def test_platform_staff_who_are_not_super_admins_are_forbidden(console_support, method, url):
    """Rafael is signed into the console and can work every other part of it.
    The jobs section reads the platform's own machinery, so it is not his."""
    response = await _call(console_support, method, url)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_the_support_desk_still_has_the_rest_of_the_console(console_support):
    assert (await console_support.get("/v1/platform/tenants")).status_code == 200


async def test_a_super_admin_token_is_refused_on_a_clinic_address(console, client):
    """Hiding the menu item is the front end's job; this is the back end's.
    The console's address is part of the check, so a token lifted out of it
    and replayed at a clinic does not become a way in."""
    response = await client.get(
        JOBS, headers={"Host": NORTHGATE, "Authorization": f"Bearer {console.token}"}
    )
    assert response.status_code == 403


async def test_a_support_session_inside_a_clinic_is_not_a_way_back_to_the_jobs(console, client):
    """Platform staff working inside a clinic hold a platform subject and a
    tenant at once. That token opens that clinic, and nothing on the console."""
    tenants = (await console.get("/v1/platform/tenants")).json()
    opened = await console.post(f"/v1/platform/tenants/{tenants[0]['id']}/impersonate")
    assert opened.status_code == 200

    response = await client.get(
        JOBS,
        headers={"Host": CONSOLE, "Authorization": f"Bearer {opened.json()['access_token']}"},
    )
    assert response.status_code == 403


# --- what a super admin gets -------------------------------------------------


async def test_a_super_admin_reads_the_section_even_with_the_monitor_down(console):
    """The page renders and says the monitor is unreachable, rather than
    answering 500 or — worse — a wall of zeroes that reads like a quiet
    platform."""
    response = await console.get(JOBS)
    assert response.status_code == 200

    body = response.json()
    assert body["available"] is False
    assert body["problem"]
    assert body["items"] == [] and body["total"] == 0
    assert body["summary"]["workers_online"] == 0


async def test_one_job_that_the_monitor_cannot_produce_is_a_not_found(console):
    response = await console.get("/v1/platform/jobs/tasks/does-not-exist")
    assert response.status_code == 404


async def test_the_proxy_reports_a_monitor_that_is_not_answering(console):
    """Authorised, and there is still nothing behind it. 502 is the honest
    answer: the caller is allowed, the upstream is not there."""
    response = await console.get(FLOWER)
    assert response.status_code == 502


# --- the cookie that lets a browser load Flower's own pages ------------------


async def test_the_flower_cookie_opens_the_proxy_and_nothing_else(console, client):
    opened = await console.post("/v1/platform/jobs/flower-session")
    assert opened.status_code == 200
    assert opened.json()["url"].endswith("/")

    cookie = client.cookies.get("pms_flower")
    assert cookie, "the session should be set as a cookie, not returned in the body"

    # It carries the proxy, with no Authorization header at all — which is the
    # whole point: a stylesheet request cannot send one.
    through = await client.get(FLOWER, headers={"Host": CONSOLE})
    assert through.status_code == 502  # allowed through; the monitor is down

    # And it is not a console session. Same person, different door.
    elsewhere = await client.get(
        "/v1/platform/tenants",
        headers={"Host": CONSOLE, "Authorization": f"Bearer {cookie}"},
    )
    assert elsewhere.status_code == 401


async def test_the_flower_cookie_copied_into_a_header_is_still_held_to_the_address(
    console, client,
):
    """The cookie is the one credential whose address is not re-checked, and it
    is host-only so the browser never offers it anywhere else. Lifted out and
    replayed as a bearer token it loses that exemption."""
    await console.post("/v1/platform/jobs/flower-session")
    cookie = client.cookies.get("pms_flower")

    response = await client.get(
        FLOWER, headers={"Host": NORTHGATE, "Authorization": f"Bearer {cookie}"}
    )
    assert response.status_code == 403


async def test_a_scoped_token_is_not_a_clinic_session(client):
    """The cookie names a platform subject, so it could never open a clinic
    anyway. This pins the general rule it relies on: a scoped token is not a
    session, whoever it names."""
    import uuid

    signed = await sign_in(client, "northgate_admin")
    scoped = create_access_token(
        subject_id=uuid.UUID(signed.user["id"]),
        subject_kind="user",
        session_id=uuid.uuid4(),
        tenant_id=uuid.UUID(signed.tenant_id),
        tenant_slug="northgate",
        role="tenant_admin",
        scope="jobs:flower",
    )
    response = await client.get(
        "/v1/patients", headers={"Host": NORTHGATE, "Authorization": f"Bearer {scoped}"}
    )
    assert response.status_code == 401


async def test_signing_the_flower_session_out_takes_the_cookie_back(console, client):
    await console.post("/v1/platform/jobs/flower-session")
    assert client.cookies.get("pms_flower")

    closed = await console.delete("/v1/platform/jobs/flower-session")
    assert closed.status_code == 204
    assert not client.cookies.get("pms_flower")


# --- shaping Flower's answer -------------------------------------------------
#
# Captured from Flower 2.x. These are the only tests that know its vocabulary,
# which is the point of keeping the translation in one module.

TASKS = {
    "a1": {
        "uuid": "a1", "name": "pms.support.deliver", "state": "SUCCESS",
        "args": "('t-1', 'r-9')", "kwargs": "{}", "worker": "celery@worker-1",
        "routing_key": "pms", "received": 1_760_000_000.0, "started": 1_760_000_001.0,
        "succeeded": 1_760_000_003.5, "runtime": 2.5, "retries": 0, "result": "'sent'",
    },
    "b2": {
        "uuid": "b2", "name": "pms.support.deliver", "state": "FAILURE",
        "args": "('t-2', 'r-4')", "kwargs": "{}", "worker": "celery@worker-2",
        "received": 1_760_000_010.0, "started": 1_760_000_011.0, "failed": 1_760_000_012.0,
        "runtime": 1.0, "retries": 2, "exception": "SMTPException('refused')",
        "traceback": "Traceback (most recent call last):\\n  ...",
    },
    "c3": {
        "uuid": "c3", "name": "pms.support.sweep", "state": "STARTED",
        "args": "()", "kwargs": "{}", "worker": "celery@worker-1",
        "received": 1_760_000_020.0, "started": 1_760_000_021.0,
    },
    "d4": {"uuid": "d4", "name": "pms.support.sweep", "state": "RECEIVED", "args": "()"},
    "e5": {"uuid": "e5", "name": "pms.support.deliver", "state": "RETRY", "args": "()"},
}

# `GET /workers?json=1`. The running totals really do arrive hyphenated —
# Flower names them after the Celery events they count.
DASHBOARD = {
    "data": [
        {"hostname": "celery@worker-1", "status": True, "active": 1, "processed": 40,
         "task-succeeded": 38, "task-failed": 1, "task-retried": 1,
         "loadavg": [0.1, 0.2, 0.3]},
        {"hostname": "celery@worker-2", "status": False, "active": 0, "processed": 9,
         "succeeded": 8, "failed": 1, "retried": 0, "loadavg": []},
    ]
}

WORKER_DETAIL = {
    "celery@worker-1": {
        "stats": {"pool": {"max-concurrency": 4}},
        "active_queues": [{"name": "pms"}],
    }
}


def _jobs():
    return sorted(
        (jobs_service._job(i, raw) for i, raw in jobs_service._tasks(TASKS)),
        key=jobs_service._sort_key,
        reverse=True,
    )


def test_celery_states_fold_into_the_six_buckets():
    by_id = {job.id: job for job in _jobs()}
    assert by_id["a1"].status == "completed"
    assert by_id["b2"].status == "failed"
    assert by_id["c3"].status == "active"
    assert by_id["d4"].status == "pending"   # RECEIVED is queued, not running
    assert by_id["e5"].status == "retried"
    # the raw state survives, because the buckets lose detail on purpose
    assert by_id["d4"].state == "RECEIVED"


def test_a_finished_job_carries_its_duration_and_its_error():
    by_id = {job.id: job for job in _jobs()}
    assert by_id["a1"].runtime_seconds == 2.5
    assert by_id["a1"].running is False
    assert by_id["a1"].finished_at == "2025-10-09T08:53:23+00:00"
    assert by_id["b2"].exception == "SMTPException('refused')"
    assert by_id["b2"].retries == 2
    # the traceback and the result belong to the single-job read, not the list
    assert by_id["b2"].traceback is None and by_id["a1"].result is None


def test_a_running_job_reports_how_long_it_has_been_going():
    running = jobs_service._job("c3", TASKS["c3"])
    assert running.running is True
    assert running.runtime_seconds and running.runtime_seconds > 0


def test_the_worker_list_merges_the_dashboard_with_the_inspection():
    workers = jobs_service._workers(DASHBOARD, WORKER_DETAIL)
    assert [w.name for w in workers] == ["celery@worker-1", "celery@worker-2"]

    first, second = workers
    assert first.online is True and first.concurrency == 4 and first.queues == ["pms"]
    assert (first.succeeded, first.failed, first.retried) == (38, 1, 1)
    # worker-2 was never inspected; it still renders, without a concurrency —
    # and its counters arrive unhyphenated, which is also read.
    assert second.online is False and second.concurrency is None
    assert (second.succeeded, second.failed) == (8, 1)


def test_the_workers_are_re_inspected_now_and_then_but_not_every_poll():
    """Flower inspects its workers once, at startup, and then only when asked.
    In a container set the monitor comes up before the workers do, so that one
    inspection finds nothing — without this the concurrency and queue columns
    would be empty for the life of the process. The other half matters too: a
    screen polling every few seconds must not ping every worker each time."""
    jobs_service._inspected_at = 0.0

    assert jobs_service._due_an_inspection() is True, "the first read must inspect"
    assert jobs_service._due_an_inspection() is False, "the poll behind it must not"

    # far enough in the past to be due again
    jobs_service._inspected_at -= jobs_service.settings.flower_inspect_seconds + 1
    assert jobs_service._due_an_inspection() is True


def test_the_worker_list_survives_an_inspection_that_failed():
    workers = jobs_service._workers(DASHBOARD, None)
    assert len(workers) == 2 and all(w.concurrency is None for w in workers)


def test_the_tiles_count_the_whole_window():
    summary = jobs_service._summary(_jobs(), jobs_service._workers(DASHBOARD, WORKER_DETAIL))
    assert summary.completed == 1
    assert summary.failed == 1
    assert summary.active == 1
    assert summary.pending == 1
    assert summary.retried == 1
    assert summary.workers_online == 1 and summary.workers_total == 2
    assert summary.window == 5


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"status": "failed"}, {"b2"}),
        ({"task": "pms.support.sweep"}, {"c3", "d4"}),
        ({"worker": "celery@worker-1"}, {"a1", "c3"}),
        ({"search": "t-2"}, {"b2"}),
        ({"search": "SMTPEXCEPTION"}, {"b2"}),      # case is not a filter
        ({"status": "failed", "worker": "celery@worker-1"}, set()),
    ],
)
def test_the_filters_narrow_the_page(filters, expected):
    from app.models.enums import JobStatus

    kept = {
        job.id
        for job in _jobs()
        if jobs_service._matches(
            job,
            status=JobStatus(filters["status"]) if "status" in filters else None,
            task=filters.get("task"),
            worker=filters.get("worker"),
            since=None,
            until=None,
            search=filters.get("search", ""),
        )
    }
    assert kept == expected


def test_a_date_window_is_read_in_utc():
    from datetime import datetime

    jobs = _jobs()
    since = jobs_service._aware(datetime(2025, 10, 9, 8, 53, 25))
    kept = {
        job.id
        for job in jobs
        if jobs_service._matches(
            job, status=None, task=None, worker=None, since=since, until=None, search=""
        )
    }
    # a1 finished at 08:53:23 and falls outside; the two later ones do not.
    assert "a1" not in kept and {"b2", "c3"} <= kept


# --- what a job is, in words ---------------------------------------------------


def _one(name: str, *, args: str = "()", kwargs: str = "{}"):
    return jobs_service._job("x", {"uuid": "x", "name": name, "state": "SUCCESS", "args": args, "kwargs": kwargs})


def test_every_registered_task_has_a_label():
    from app.worker import tasks  # noqa: F401 - registers them
    from app.worker.celery_app import celery_app

    registered = {t for t in celery_app.tasks if t.startswith("pms.")}
    assert registered <= set(jobs_service.TASK_LABELS), registered - set(jobs_service.TASK_LABELS)


def test_an_email_job_is_named_for_what_it_sends():
    reset = _one("pms.invitations.deliver", kwargs="{'tenant_id': 't', 'user_id': 'u', 'purpose': 'reset_password'}")
    invite = _one("pms.invitations.deliver", kwargs="{'purpose': 'invite'}")
    # sent positionally, before arguments went by name
    old = _one("pms.invitations.deliver", args="('t', 'u', 'reset', '', '::1', 'Mozilla/5.0')")
    assert (reset.label, invite.label, old.label) == ("Password reset email", "Invitation email", "Password reset email")
    assert _one("pms.support.deliver").label == "Support request email"
    assert _one("pms.something.new").label == "pms.something.new"


async def test_arguments_are_labelled_and_never_show_the_address_or_browser():
    old = _one("pms.invitations.deliver", args="('t-1', 'u-1', 'reset', '', '::1', 'Mozilla/5.0')")
    await jobs_service._describe(None, [old])
    assert [(a.label, a.value) for a in old.arguments] == [
        ("Email", "Password reset"), ("Clinic", "t-1"), ("Account", "u-1"),
    ]

    named = _one(
        "pms.imports.run", kwargs="{'tenant_id': 't-2', 'job_id': 'j-7', 'note': 'x'}"
    )
    await jobs_service._describe(None, [named])
    assert [(a.label, a.value) for a in named.arguments] == [("Clinic", "t-2"), ("Import", "j-7"), ("Note", "x")]

    # a repr that is not one is shown as nothing, and never run
    odd = _one("pms.support.deliver", kwargs="__import__('os').system('true')")
    await jobs_service._describe(None, [odd])
    assert odd.arguments == []


async def test_clinics_and_accounts_are_shown_by_name(northgate):
    from app.db.session import session_factory

    me = next(u for u in (await northgate.get("/v1/users")).json() if u["email"] == "admin@northgate.clinic")
    job = _one(
        "pms.invitations.deliver",
        kwargs=f"{{'tenant_id': '{northgate.tenant_id}', 'user_id': '{me['id']}', 'purpose': 'invite', "
        "'requested_by': 'Ines <ines@astrale.health>'}",
    )
    async with session_factory() as session:
        await jobs_service._describe(session, [job])
    assert dict((a.label, a.value) for a in job.arguments) == {
        "Email": "Invitation",
        "Clinic": "Northgate Dental Studio",
        "Account": f"{me['name']} <admin@northgate.clinic>",
        "Requested by": "Ines <ines@astrale.health>",
    }
