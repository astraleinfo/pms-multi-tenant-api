"""Nobody is ever handed a password.

An account is created without one, its owner is emailed a single-use link, and
the password it ends up with is one only they have seen. The fallback — an
admin typing a password for a colleague at the desk — still exists, and this
file pins down the part that makes it safe: whoever gets a password they did
not choose is held at the change-password screen until they replace it.

Under the `console` mail transport the API hands the caller the URL it would
have emailed, which is how these tests read an inbox they do not have.
"""

from __future__ import annotations

import pytest

from tests.conftest import CLEARVIEW, NORTHGATE, audit_lines, token_from


async def _invite_colleague(admin, client, *, email: str, name: str = "Nadia Rahman"):
    """Create a colleague with no password, and dig their link out of the log."""
    created = await admin.post(
        "/v1/users", json={"name": name, "email": email, "role": "receptionist"}
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["status"] == "invited"
    assert row["invitation_pending"] is True

    # The console transport does not return the link on this route, so the
    # token is read the way an operator would: by asking for it again.
    return row


# --- §1 and §2: the invitation is the way in ---------------------------------


async def test_a_new_colleague_gets_a_link_and_chooses_their_own_password(
    northgate, client, monkeypatch
):
    sent: list = []

    from app.services import mailer

    async def capture(email):
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)

    row = await _invite_colleague(
        northgate, client, email="nadia.rahman@northgate.clinic"
    )
    assert len(sent) == 1
    assert sent[0].to == "nadia.rahman@northgate.clinic"
    assert "Set up your" in sent[0].subject

    token = token_from(_url_in(sent[0].text))

    # Until they act on it, there is no password and no way in.
    refused = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "nadia.rahman@northgate.clinic", "password": "anything-at-all-2026"},
    )
    assert refused.status_code == 401

    preview = (
        await client.get(
            "/v1/auth/invitation", headers={"Host": NORTHGATE}, params={"token": token}
        )
    ).json()
    assert preview["valid"] is True
    assert preview["purpose"] == "invite"
    assert preview["clinic"].startswith("Northgate")

    accepted = await client.post(
        "/v1/auth/password/reset",
        headers={"Host": NORTHGATE},
        json={"token": token, "password": "nadia-chose-this-2026"},
    )
    assert accepted.status_code == 200, accepted.text

    session = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "nadia.rahman@northgate.clinic", "password": "nadia-chose-this-2026"},
    )
    assert session.status_code == 200
    assert session.json()["must_change_password"] is False
    assert session.json()["user"]["status"] == "active"

    trail = await audit_lines(northgate.tenant_id, action="auth.invitation_accepted")
    assert trail, "accepting an invitation is recorded"

    _ = row


def _url_in(body: str) -> str:
    for line in body.splitlines():
        if line.strip().startswith("http"):
            return line.strip()
    raise AssertionError(f"no link in message:\n{body}")


async def test_a_password_an_admin_typed_must_be_replaced_before_anything_else(
    northgate, client
):
    """The fallback path, and the flag that keeps it honest.

    The admin knows this password, so the account is not really its owner's
    yet. Sign-in succeeds — refusing it would leave them with no way past —
    but the session says `must_change_password` until they choose their own.
    """
    created = await northgate.post(
        "/v1/users",
        json={
            "name": "Tom Bright",
            "email": "tom.bright@northgate.clinic",
            "role": "receptionist",
            "password": "admin-typed-this-2026",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "active"
    assert created.json()["must_change_password"] is True

    session = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "tom.bright@northgate.clinic", "password": "admin-typed-this-2026"},
    )
    assert session.status_code == 200
    assert session.json()["must_change_password"] is True

    token = session.json()["access_token"]
    headers = {"Host": NORTHGATE, "Authorization": f"Bearer {token}"}

    me = (await client.get("/v1/auth/me", headers=headers)).json()
    assert me["must_change_password"] is True

    # The wrong current password does not get them past it.
    wrong = await client.post(
        "/v1/auth/password/change",
        headers=headers,
        json={"current_password": "not-the-one-2026", "new_password": "tom-chose-this-2026"},
    )
    assert wrong.status_code == 401

    # Neither does re-using the one they were given.
    same = await client.post(
        "/v1/auth/password/change",
        headers=headers,
        json={
            "current_password": "admin-typed-this-2026",
            "new_password": "admin-typed-this-2026",
        },
    )
    assert same.status_code == 422

    changed = await client.post(
        "/v1/auth/password/change",
        headers=headers,
        json={
            "current_password": "admin-typed-this-2026",
            "new_password": "tom-chose-this-2026",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["must_change_password"] is False

    # The session that did it survives; the old password does not.
    assert (await client.get("/v1/auth/me", headers=headers)).status_code == 200
    stale = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "tom.bright@northgate.clinic", "password": "admin-typed-this-2026"},
    )
    assert stale.status_code == 401


async def test_setting_your_own_password_is_not_forced_to_change_it(northgate, client):
    """You chose it and nobody else saw it, so there is nothing to replace.

    The account here is one this test creates, never the shared fixture's: an
    admin's seeded password is shorter than this API now accepts, so rotating
    it would be a change no test could put back.
    """
    created = await northgate.post(
        "/v1/users",
        json={
            "name": "Rota Admin",
            "email": "rota.admin@northgate.clinic",
            "role": "tenant_admin",
            "password": "handed-to-them-2026",
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    assert created.json()["must_change_password"] is True

    session = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "rota.admin@northgate.clinic", "password": "handed-to-them-2026"},
    )
    assert session.status_code == 200
    headers = {
        "Host": NORTHGATE,
        "Authorization": f"Bearer {session.json()['access_token']}",
    }

    # Setting your own password through the admin route clears the flag too:
    # the reason it was raised was that somebody else had typed the last one.
    updated = await client.post(
        f"/v1/users/{user_id}/password",
        headers=headers,
        json={"password": "rota-chose-this-2026"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["must_change_password"] is False


# --- forgotten passwords -----------------------------------------------------


async def test_asking_for_a_reset_says_the_same_thing_either_way(client):
    """The form cannot be used to find out who works at a clinic."""
    known = await client.post(
        "/v1/auth/password/forgot",
        headers={"Host": NORTHGATE},
        json={"email": "admin@northgate.clinic"},
    )
    unknown = await client.post(
        "/v1/auth/password/forgot",
        headers={"Host": NORTHGATE},
        json={"email": "nobody-at-all@northgate.clinic"},
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


async def test_a_reset_link_ends_every_session_the_account_had(northgate, client, monkeypatch):
    """The point of a reset is usually that somebody else knew the password."""
    sent: list = []

    from app.services import mailer

    async def capture(email):
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)

    created = await northgate.post(
        "/v1/users",
        json={
            "name": "Wendy Cole",
            "email": "wendy.cole@northgate.clinic",
            "role": "doctor",
            "password": "wendy-first-password-2026",
        },
    )
    assert created.status_code == 201, created.text

    signed_in = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "wendy.cole@northgate.clinic", "password": "wendy-first-password-2026"},
    )
    assert signed_in.status_code == 200
    held = {
        "Host": NORTHGATE,
        "Authorization": f"Bearer {signed_in.json()['access_token']}",
    }
    assert (await client.post("/v1/auth/refresh", headers=held)).status_code == 200

    asked = await client.post(
        "/v1/auth/password/forgot",
        headers={"Host": NORTHGATE},
        json={"email": "wendy.cole@northgate.clinic"},
    )
    assert asked.status_code == 202
    assert len(sent) == 1
    assert "Reset your" in sent[0].subject

    token = token_from(_url_in(sent[0].text))
    done = await client.post(
        "/v1/auth/password/reset",
        headers={"Host": NORTHGATE},
        json={"token": token, "password": "wendy-reset-this-2026"},
    )
    assert done.status_code == 200, done.text

    # The refresh chain that browser was holding is gone. Access tokens already
    # minted run out on their own; what cannot happen is renewing one.
    assert (await client.post("/v1/auth/refresh", headers=held)).status_code == 401

    # And the password it was opened with is no longer a way back in.
    stale = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "wendy.cole@northgate.clinic", "password": "wendy-first-password-2026"},
    )
    assert stale.status_code == 401


async def test_issuing_a_new_link_kills_the_previous_one(northgate, client, monkeypatch):
    """An old email in an inbox stops working the moment a new one is sent."""
    sent: list = []

    from app.services import mailer

    async def capture(email):
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)

    created = await northgate.post(
        "/v1/users",
        json={"name": "Priya Shah", "email": "priya.shah@northgate.clinic", "role": "doctor"},
    )
    assert created.status_code == 201, created.text
    first = token_from(_url_in(sent[0].text))

    resent = await northgate.post(f"/v1/users/{created.json()['id']}/invite")
    assert resent.status_code == 200, resent.text
    second = token_from(_url_in(sent[1].text))
    assert first != second

    dead = await client.post(
        "/v1/auth/password/reset",
        headers={"Host": NORTHGATE},
        json={"token": first, "password": "priya-chose-this-2026"},
    )
    assert dead.status_code == 422

    alive = await client.post(
        "/v1/auth/password/reset",
        headers={"Host": NORTHGATE},
        json={"token": second, "password": "priya-chose-this-2026"},
    )
    assert alive.status_code == 200, alive.text


# --- what a link is not ------------------------------------------------------


async def test_a_link_is_inert_at_another_clinics_address(northgate, client, monkeypatch):
    """The clinic comes from the host here exactly as it does for sign-in."""
    sent: list = []

    from app.services import mailer

    async def capture(email):
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)

    await northgate.post(
        "/v1/users",
        json={"name": "Owen Blake", "email": "owen.blake@northgate.clinic", "role": "doctor"},
    )
    token = token_from(_url_in(sent[0].text))

    elsewhere = await client.get(
        "/v1/auth/invitation", headers={"Host": CLEARVIEW}, params={"token": token}
    )
    assert elsewhere.status_code == 200
    assert elsewhere.json()["valid"] is False

    used = await client.post(
        "/v1/auth/password/reset",
        headers={"Host": CLEARVIEW},
        json={"token": token, "password": "wrong-clinic-entirely-2026"},
    )
    assert used.status_code == 422


@pytest.mark.parametrize("token", ["", "x" * 60, "not-a-real-token-at-all-0000000000"])
async def test_an_unusable_link_is_a_page_not_an_error(client, token):
    response = await client.get(
        "/v1/auth/invitation", headers={"Host": NORTHGATE}, params={"token": token}
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["problem"]


async def test_a_locked_account_is_not_released_by_asking_for_a_link(
    northgate, client, monkeypatch
):
    """Releasing a lockout is an admin's decision, not an email's."""
    sent: list = []

    from app.services import mailer

    async def capture(email):
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)

    created = await northgate.post(
        "/v1/users",
        json={
            "name": "Locked Out",
            "email": "locked.out@northgate.clinic",
            "role": "receptionist",
            "password": "temporary-for-now-2026",
        },
    )
    user_id = created.json()["id"]
    await northgate.patch(f"/v1/users/{user_id}/status", json={"status": "locked"})

    asked = await client.post(
        "/v1/auth/password/forgot",
        headers={"Host": NORTHGATE},
        json={"email": "locked.out@northgate.clinic"},
    )
    assert asked.status_code == 202, "the answer is the same as always"
    assert sent == [], "but nothing was sent"

    resend = await northgate.post(f"/v1/users/{user_id}/invite")
    assert resend.status_code == 409


async def test_a_shorter_invitation_ttl_reaches_the_actual_token(
    northgate, client, console, monkeypatch
):
    """The token's real expiry and the email's stated expiry must move
    together — both are read from one resolved `Timings` value now, not two
    independent reads that could disagree."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.db.session import tenant_session
    from app.models.enums import AuthTokenPurpose
    from app.models.identity import AuthToken
    from app.services import mailer

    sent: list = []

    async def capture(email):
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)

    await console.patch("/v1/platform/settings", json={"invite_token_ttl_seconds": 86400})

    before = datetime.now(timezone.utc)
    row = await _invite_colleague(northgate, client, email="fast.expiry@northgate.clinic")

    assert len(sent) == 1
    assert "expires in a day" in sent[0].text  # mailer.invitation()'s wording for 86400s

    tenant_id = uuid.UUID((await northgate.get("/v1/auth/me")).json()["tenant_id"])
    async with tenant_session(tenant_id) as session:
        token = (
            await session.execute(
                select(AuthToken).where(
                    AuthToken.subject_id == uuid.UUID(row["id"]),
                    AuthToken.purpose == AuthTokenPurpose.INVITE,
                )
            )
        ).scalar_one()

    delta = token.expires_at - before
    assert timedelta(hours=23) < delta < timedelta(hours=25), f"expected ~24h, got {delta}"
