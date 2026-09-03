"""Drive the counter flow the way the screens drive it, and print each step.

This is the sequence the pharmacy window and the billing desk actually perform,
in the order they perform it — dispense, look at the account, raise the
counter's invoice, take the money — against the real application over ASGI, with
no server to start. It exists because "it does not work" is faster to diagnose
by watching the requests than by reading the components.

    python scripts/check_counter_flow.py

It writes to whatever database the environment points at, so point it at a test
one. Defaults to `pms_new_test` on 5433.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = os.environ.get(
    "CHECK_DATABASE_URL",
    "postgresql+asyncpg://pms_app:app-password-change-me@localhost:5433/pms_new_test",
)
MIGRATION_DB = os.environ.get(
    "CHECK_MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://pms_owner:owner-password-change-me@localhost:5433/pms_new_test",
)

os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": DB,
        "MIGRATION_DATABASE_URL": MIGRATION_DB,
        "JWT_SECRET": "check-secret-not-used-anywhere-real-0000",
        "APP_DOMAIN": "localhost",
        "COOKIE_SECURE": "false",
        "ALLOW_TENANT_HEADER": "false",
        "MAIL_PROVIDER": "console",
        "API_RATE_LIMIT": "100000/minute",
        "LOGIN_RATE_LIMIT": "1000/minute",
    }
)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app  # noqa: E402

HOST = "northgate.localhost"
ADMIN = ("admin@northgate.clinic", "admin-2026")
DOCTOR = ("a.iyer@northgate.clinic", "aiyer-2026")

OK = "  ok  "
BAD = " FAIL "


def say(step: str, detail: str = "") -> None:
    print(f"{step:<52} {detail}")


class Session:
    def __init__(self, client: AsyncClient, token: str) -> None:
        self._client = client
        self._headers = {"Host": HOST, "Authorization": f"Bearer {token}"}

    async def request(self, method: str, url: str, **kw):
        response = await self._client.request(method, url, headers=self._headers, **kw)
        if response.status_code >= 400:
            print(f"\n{BAD} {method} {url} -> {response.status_code}")
            print(response.text[:600])
            raise SystemExit(1)
        return response.json() if response.content else None

    get = lambda self, url, **kw: self.request("GET", url, **kw)  # noqa: E731
    post = lambda self, url, **kw: self.request("POST", url, **kw)  # noqa: E731


async def sign_in(client: AsyncClient, email: str, password: str) -> Session:
    response = await client.post(
        "/v1/auth/login", headers={"Host": HOST}, json={"email": email, "password": password}
    )
    if response.status_code != 200:
        print(f"{BAD} sign-in as {email} -> {response.status_code}")
        print(response.text[:400])
        raise SystemExit(1)
    return Session(client, response.json()["access_token"])


async def main() -> None:
    print(f"\ndatabase: {DB}\n")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        admin = await sign_in(client, *ADMIN)
        doctor = await sign_in(client, *DOCTOR)
        say("signed in", "practice admin + clinician")

        # --- what the clinic charges -------------------------------------
        prices = await admin.get("/v1/pricing/services", params={"kind": "lab"})
        say("tariff readable", f"{len(prices)} lab prices, CBCT = "
            f"{next(p['price'] for p in prices if p['code'] == 'CBCT')}")

        # --- a stock line with a selling price ---------------------------
        item = await admin.post(
            "/v1/inventory/items",
            json={
                "sku": f"FLOW-{os.getpid() % 10000}",
                "name": "Flow check tablets",
                "strength": "500 mg",
                "unit": "tab",
                "category": "medicine",
                "unit_price": 10,
            },
        )
        await admin.post(
            "/v1/inventory/transactions",
            json={"item_id": item["id"], "type": "purchase", "qty": 100,
                  "batch_no": "FLOW-B1", "expiry": "2028-01-31"},
        )
        say("stock line opened and stocked", f"{item['sku']} at {item['unit_price']}/tab")

        # --- a visit ------------------------------------------------------
        patient = await admin.post(
            "/v1/patients",
            json={"full_name": "Counter Flow Check", "dob": "1991-03-03",
                  "sex": "female", "phone": "+44 7700 900222"},
        )
        who = await admin.get("/v1/users/doctors")
        encounter = await doctor.post(
            "/v1/encounters",
            json={"patient_id": patient["id"], "doctor_id": who[0]["id"], "date": "2026-09-03",
                  "chief_complaint": "Counter flow", "notes": "", "diagnosis": "",
                  "clinical_ext": {}},
        )
        rx = await doctor.post(
            f"/v1/encounters/{encounter['id']}/prescriptions",
            json={"items": [{"medicine": "Flow check tablets", "strength": "500 mg",
                             "qty": 6, "item_id": item["id"]}]},
        )
        await doctor.post(
            f"/v1/encounters/{encounter['id']}/lab-orders",
            json={"tests": [{"code": "CBCT", "name": "Cone beam CT", "panel": "Imaging"}]},
        )
        say("visit recorded", "consultation + 6 tablets prescribed + CBCT ordered")

        account = await admin.get(f"/v1/billing/patients/{patient['id']}/account")
        say("account before dispensing", f"total due {account['total_due']} "
            f"(pharmacy {account['departments']['pharmacy']['due']} — nothing handed over yet)")

        # --- the pharmacy window -----------------------------------------
        line = rx["items"][0]
        await admin.post(
            f"/v1/pharmacy/prescriptions/{rx['id']}/dispense",
            json={"lines": [{"item_id": line["id"], "qty": 6, "batch_no": "FLOW-B1"}]},
        )
        say("dispensed", "6 tab out of the ledger")

        account = await admin.get(f"/v1/billing/patients/{patient['id']}/account")
        pharmacy = account["departments"]["pharmacy"]
        say("what the window shows", f"{pharmacy['due']} owing, "
            f"{len(pharmacy['unbilled'])} charge not billed yet")

        # This is exactly what CounterPayment does when "Take the money now"
        # is left ticked: raise the counter's invoice, then pay it.
        invoice = await admin.post(
            f"/v1/billing/patients/{patient['id']}/invoices",
            json={"department": "pharmacy", "issue": True},
        )
        say("pharmacy invoice raised", f"{invoice['number']} · {invoice['department']} · "
            f"{invoice['totals']['total']}")

        paid = await admin.post(
            f"/v1/billing/patients/{patient['id']}/payments",
            json={"department": "pharmacy", "amount": invoice["totals"]["total"], "method": "cash"},
        )
        say("payment taken at the window", f"{paid['paid']} · {paid['remaining']} left here · "
            f"invoice now {paid['invoices'][0]['status']}")

        # --- pressing it again must not charge twice ----------------------
        again = await client.post(
            f"/v1/billing/patients/{patient['id']}/invoices",
            headers={"Host": HOST, "Authorization": admin._headers["Authorization"]},
            json={"department": "pharmacy", "issue": True},
        )
        if again.status_code == 409:
            say("billed twice?", f"{OK} refused: {again.json()['error']['message']}")
        else:
            say("billed twice?", f"{BAD} it let it through ({again.status_code})")
            raise SystemExit(1)

        # --- the billing desk, later --------------------------------------
        account = await admin.get(f"/v1/billing/patients/{patient['id']}/account")
        say("account after the window", " · ".join(
            f"{d} {account['departments'][d]['due']}" for d in ("clinic", "lab", "pharmacy")
        ))

        lab_invoice = await admin.post(
            f"/v1/billing/patients/{patient['id']}/invoices",
            json={"department": "lab", "issue": True},
        )
        say("lab invoice raised at the desk",
            f"{lab_invoice['number']} · {lab_invoice['totals']['total']}")

        part = await admin.post(
            f"/v1/billing/patients/{patient['id']}/payments",
            json={"department": "lab", "amount": 500, "method": "upi"},
        )
        say("part payment on the lab", f"took 500, {part['remaining']} still owing there")

        account = await admin.get(f"/v1/billing/patients/{patient['id']}/account")
        say("pharmacy untouched by that", f"pharmacy due {account['departments']['pharmacy']['due']}")

        # --- the final settlement, at the door ----------------------------
        # What "Settle everything" does: the counters that still owe, in turn.
        # The pharmacy is not in it — it was paid at the window, so it has no
        # `due` to ask for. It still reports what it took.
        account = await admin.get(f"/v1/billing/patients/{patient['id']}/account")
        owing = [d for d in ("clinic", "lab", "pharmacy") if account["departments"][d]["due"] > 0.005]
        say("final settlement asks for", " + ".join(
            f"{d} {account['departments'][d]['due']}" for d in owing
        ) + f" = {account['total_due']}")
        say("and leaves out", f"pharmacy, {account['departments']['pharmacy']['paid']} "
            "already paid at the window")

        for department in owing:
            entry = account["departments"][department]
            if entry["unbilled"]:
                await admin.post(
                    f"/v1/billing/patients/{patient['id']}/invoices",
                    json={"department": department, "issue": True},
                )
            await admin.post(
                f"/v1/billing/patients/{patient['id']}/payments",
                json={"department": department, "amount": entry["due"], "method": "card"},
            )

        account = await admin.get(f"/v1/billing/patients/{patient['id']}/account")
        if account["total_due"] > 0.005:
            say("settled everything?", f"{BAD} {account['total_due']} still owing")
            raise SystemExit(1)
        say("settled everything?", f"{OK} nothing owing · " + " · ".join(
            f"{d} took {account['departments'][d]['paid']}" for d in ("clinic", "lab", "pharmacy")
        ))

        # --- what the account view lists ----------------------------------
        bills = await admin.get("/v1/billing/invoices", params={"patient_id": patient["id"], "limit": 50})
        say("their bills, every counter", ", ".join(
            f"{i['number']}/{i['department']}" for i in bills["items"]
        ))

        print(f"\n{OK} the whole counter flow works end to end\n")


if __name__ == "__main__":
    asyncio.run(main())
