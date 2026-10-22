"""What each hand-off puts on the wire, and what the job monitor shows of it.

Every job is sent with named arguments, so the console's job detail reads
`{'tenant_id': …, 'user_id': …}` rather than a bare tuple. The names have to be
the task's own — a name it does not take is a job that fails on arrival — and an
invitation's requester address and browser reach the worker without being put
on display.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.models.enums import AuthTokenPurpose
from app.worker import tasks

TENANT, SUBJECT = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def wire(monkeypatch) -> list[tuple[object, dict]]:
    sent: list[tuple[object, dict]] = []
    for task in (tasks.deliver_link_email, tasks.deliver_support_request, tasks.run_import):
        monkeypatch.setattr(
            task, "apply_async", lambda task=task, **options: sent.append((task, options)) or None
        )
    return sent


def _binds(task, kwargs: dict) -> None:
    inspect.signature(task.run).bind(**kwargs)


def test_an_invitation_is_named_and_shows_no_address_or_browser(wire, link_emails):
    assert link_emails.real_enqueue(
        TENANT, SUBJECT, AuthTokenPurpose.RESET, ip="203.0.113.9", user_agent="Mozilla/5.0 (Test)"
    )
    task, options = wire[-1]
    _binds(task, options["kwargs"])
    assert options["kwargs"]["ip"] == "203.0.113.9"

    shown = options["kwargsrepr"]
    assert str(SUBJECT) in shown and "'purpose': 'reset_password'" in shown
    assert options["kwargs"]["purpose"] == "reset_password"
    assert "the account holder" in shown
    assert "203.0.113.9" not in shown and "Mozilla" not in shown


def test_an_invitation_names_who_asked(wire, link_emails):
    link_emails.real_enqueue(TENANT, SUBJECT, AuthTokenPurpose.INVITE, issued_by_label="Ines <ines@astrale.health>")
    assert "Ines <ines@astrale.health>" in wire[-1][1]["kwargsrepr"]


def test_support_and_import_jobs_are_named(wire):
    assert tasks.enqueue_support_request(TENANT, SUBJECT)
    assert tasks.enqueue_import(TENANT, SUBJECT)
    for task, options in wire:
        assert "args" not in options
        _binds(task, options["kwargs"])
        assert options["kwargs"]["tenant_id"] == str(TENANT)


def test_the_worker_reads_both_names_for_a_reset():
    """`reset_password` from now on; `reset` from a job queued before the rename."""
    assert tasks._PURPOSE_FROM_WIRE["reset_password"] == AuthTokenPurpose.RESET
    assert tasks._PURPOSE_FROM_WIRE["reset"] == AuthTokenPurpose.RESET
    assert tasks._PURPOSE_FROM_WIRE["invite"] == AuthTokenPurpose.INVITE
