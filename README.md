# Astrale PMS — API

The backend for the [multi-tenant PMS front end](../pms-multi-tenant-web), built to the
contracts in that project's `pms-end-to-flow.md`. FastAPI, PostgreSQL, SQLAlchemy 2 and
Alembic; every route, query and row is scoped to exactly one clinic.

The front end's `src/lib/api/` was written as the integration seam — one async function per
endpoint in the reference document. This is the other side of that seam. Shapes match its
TypeScript types field for field, with one deliberate exception: **`User` has no `password`**.
The demo shipped credentials to the browser; a real server never does.

---

## Tenant isolation

This is the part worth reading. One clinic reaching another clinic's data is the failure this
service is built to make impossible, so the boundary is enforced four times over, by four
mechanisms that fail independently.

**1. The tenant comes from the address, and the token has to agree.**
`northgate.astrale.health` is Northgate and can never be anything else. The access token
carries the tenant it was minted for; if the host and the token disagree, the request is
refused before a single query runs. A valid Northgate session presented at Clearview's
address is not a Northgate session — it is nothing.

**2. It is never taken from the request.**
No handler reads a `tenant_id` from a body, a query parameter or a header. Request models are
`extra="forbid"`, so a planted `tenant_id` is a 422 rather than something quietly ignored, and
`tenant_id` on a new row is stamped from the session.

**3. The ORM refuses to run an unscoped query.**
`app/db/guard.py` listens on every ORM execution. A statement touching a tenant-owned table on
a session with no tenant bound raises `TenantIsolationViolation` — a 500 and a CRITICAL log
line, because it is a bug in this codebase, not a client error. When a tenant *is* bound, the
filter is appended to the statement whatever the caller wrote. Flushes are checked the same
way: writing, changing or deleting another clinic's row raises.

**4. PostgreSQL enforces it underneath all of that.**
Every tenant-owned table has `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY` and a
policy comparing `tenant_id` to `app.tenant_id`, which the request transaction sets with
`set_config(..., is_local => true)`. The API connects as `pms_app`, which does not own the
tables and does not have `BYPASSRLS` — the application starts by checking that and refuses to
boot in production if it is ever granted. The variable is transaction-local, so a pooled
connection cannot carry one clinic's identity into the next request, and an unbound session
sees *nothing*: `nullif(current_setting('app.tenant_id', true), '')::uuid` is NULL, and
comparing to NULL is NULL. **The failure mode is emptiness, never leakage.**

There is no bypass path anywhere, not even for the platform console: cross-clinic figures are
gathered one clinic at a time with that clinic bound to the session (`app/services/platform.py`).

Two supporting details, both deliberate:

* **Another clinic's record is 404, never 403.** A 403 would confirm the row exists.
* **A new tenant-owned table is not a silent hole.** Inherit `TenantScoped` and the column,
  the index, the ORM guard and the policy all follow — and `test_every_tenant_owned_table_has_a_policy`
  fails the build if a model is added without one.

---

## The four axes

The reference document's first rule is that tenancy, clinical type, entitlement and
customisation are never conflated. They live in four separate places here:

| Axis | Where | What it decides |
|---|---|---|
| **Tenant** | `tenant_id` on every row, `app.tenant_id`, the RLS policies | which rows exist at all |
| **Clinical type** | `Tenant.clinical_type`, `clinical_ext`, `resolve_schema` | which fields and which exam payload |
| **Entitlement** | `TenantFeature`, `require_feature(...)` | which routes answer at all |
| **Customisation** | tenant-scoped `FieldDefinition` rows | extra fields on the dynamic forms |

`is_feature_active` never reads `clinical_type`; `resolve_schema` never reads an entitlement.

---

## The middleware stack (§4)

Applied in this order, as dependencies in `app/api/deps.py`:

1. **Auth** — resolve the subject from the token alone.
2. **Tenant isolation** — bind the session to that clinic; from here the database enforces it.
3. **Feature guard** — `require_feature("billing")` on every purchasable module.
4. **RBAC** — `require_permission(Permission.PATIENT_WRITE)`. Handlers name a permission, never
   a role, so roles are a table (`app/core/permissions.py`) rather than a sweep through routers.
5. **Schema resolver** — common → clinical_type → tenant, merged on `field_key`, for the
   dynamic-form routes.

---

## Running it

```bash
make install     # virtualenv + dependencies
make db          # PostgreSQL in docker, with both roles created
make migrate     # schema, then the row-level security policies
make seed        # the three demo clinics from the front end's seed
make run         # http://localhost:8000 — docs at /docs
```

Or `make up` for all of it, or `docker compose up` to run the API in a container too.

That path uses the throwaway PostgreSQL in `docker-compose.yml`, which bootstraps itself. To
point the API at a PostgreSQL you already run — or to understand the two roles it connects as —
see [The database](#the-database-one-database-two-roles) below.

The seed is the web app's own dataset, shifted so its pinned day (2026-08-19) lands on the day
you run it — so the waiting room has a queue this morning, today's clinic list has appointments,
and the expiry watch has something to watch. Dates of birth are left where they are.

Behind that, `app/seed/history.py` writes six weeks of past: appointments kept, missed and
cancelled, the tokens they produced with real waiting and consulting times, the encounters that
came of them and the invoices some of them raised. It is seeded from the clinic's slug, so the
same past comes back every time — and because the reports read those rows rather than a
client-side guess, every panel agrees with every other one.

The demo accounts are the ones the front end documents — the email's local part without dots,
then `-2026`:

| Address | Account | Password |
|---|---|---|
| `northgate.localhost` | `admin@northgate.clinic` (practice admin) | `admin-2026` |
| `northgate.localhost` | `a.iyer@northgate.clinic` (clinician) | `aiyer-2026` |
| `clearview.localhost` | `admin@clearview.eye` | `admin-2026` |
| `rowan.localhost` | `owner@rowanskin.co` | `owner-2026` |
| `localhost` (console) | `ines@astrale.health` | `ines-2026` |

They are hashed with Argon2 on the way into the database. Five wrong passwords lock an account;
a practice admin releases it from `PATCH /v1/users/{id}/status`, which also clears the counter
and, when locking, revokes every session that account holds.

An admin gives a colleague a password with `POST /v1/users/{id}/password` — no current password,
because the point of the route is that they do not know it. The first password an account is given
is what accepts its invitation, since an `invited` row has no hash and the sign-in refuses it by
name. Every session opened with the old password is revoked, except the one making the request when
an admin is rotating their own. It is not an unlock: a locked account stays locked, or a reset
could quietly reinstate someone who had been shut out.

```bash
curl -H 'Host: northgate.localhost' -H 'Content-Type: application/json' \
     -d '{"email":"admin@northgate.clinic","password":"admin-2026"}' \
     localhost:8000/v1/auth/login
```

---

## The database: one database, two roles

**One PostgreSQL database. Two PostgreSQL roles.** This is the part people misread, so it is
worth stating flatly: there is **not** a migration database and an application database. There is
one database, and two roles that connect to it with very different privileges.

| Role | Used by | May |
|---|---|---|
| **`pms_owner`** | Alembic and the seeder, nothing else | own the schema and the tables; create, alter and drop objects; create and drop RLS policies |
| **`pms_app`** | the running FastAPI application, nothing else | `SELECT`, `INSERT`, `UPDATE`, `DELETE` rows — and nothing structural |

Both roles authenticate against the same database and see the same tables. The difference is
entirely in what PostgreSQL will let each one do, and that difference is what makes the database
the last line of tenant isolation rather than a bystander.

### Which database name

There are two supported local paths, and they differ only in where the server is and what the
database is called:

| Path | Server | Database | Bootstrapped by |
|---|---|---|---|
| `make db` / `docker compose` | `localhost:55432` | `pms` (and `pms_test` for the suite) | `scripts/bootstrap_db.sql`, run automatically on first boot |
| Your own local PostgreSQL | `localhost:5432` | `pms_api` | by hand — see [Creating it from scratch](#creating-the-database-and-roles-from-scratch) |

Pick one and set both URLs to match it. The examples below use `pms_api` on `5432`; substitute
`pms` on `55432` if you are on the docker path. `pms_test` is a separate database used only by the
test suite — it is not part of the two-role split.

### Environment variables

```env
DATABASE_URL=postgresql+asyncpg://pms_app:<app-password>@localhost:5432/pms_api

MIGRATION_DATABASE_URL=postgresql+asyncpg://pms_owner:<owner-password>@localhost:5432/pms_api
```

* **`DATABASE_URL`** is what the running FastAPI application uses. It is read once, at import, by
  `app/db/session.py`, which builds the single pooled engine every request borrows a connection
  from.
* **`MIGRATION_DATABASE_URL`** is what Alembic (`alembic/env.py`) and the seeder
  (`scripts/seed.py`) use. Both build a short-lived engine and dispose of it when they are done.
* **Both point at the same database.** Same host, same port, same name. Only the role and password
  differ. If the two URLs disagree on anything else, migrations are being applied to a database the
  API never reads.
* **The API must never use `pms_owner`.** `app/core/config.py` refuses to start a production
  process when `pms_owner` appears in `DATABASE_URL`, and `app/main.py` additionally queries
  `pg_roles` at startup and refuses to boot if the role it actually connected as holds `BYPASSRLS`.
* **The API must not have `MIGRATION_DATABASE_URL` in its production environment at all.** Nothing
  under `app/` reads it — only `alembic/env.py`, `scripts/seed.py` and `tests/conftest.py` do — so
  in production it is an unused credential, and an unused credential is pure liability. See
  [Why the roles are separated](#why-the-roles-are-separated).

### Driver requirement

Both connections **must** use `postgresql+asyncpg://`.

```text
postgresql+asyncpg://    ✅
postgresql+psycopg2://   ❌ — raises InvalidRequestError at startup
```

The application engine is built with `create_async_engine` (`app/db/session.py`), and Alembic is
async too: `alembic/env.py` uses `async_engine_from_config` and drives it with `asyncio.run`. A
synchronous driver fails immediately with *"The asyncio extension requires an async driver to be
used"* — it does not degrade quietly. `psycopg2` is not a valid driver for either URL in this
project.

URL-encode anything exotic in the password (`%40` for `@`, `%3A` for `:`).

### The two roles in detail

**`pms_owner`**

* `LOGIN` role.
* Owns the `pms_api` database, the `public` schema, and every application table Alembic creates.
* Used by Alembic migrations and by `scripts/seed.py`.
* Can create and alter database objects, and can create, alter and drop RLS policies.
* Must **not** be used by the API.

**`pms_app`**

* `LOGIN` role.
* `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, **`NOBYPASSRLS`**.
* Used only by the FastAPI runtime.
* Can perform application CRUD: `SELECT`, `INSERT`, `UPDATE`, `DELETE`.
* Must **not** own application tables.
* Must **not** be able to modify the schema or any RLS policy.

Two details worth knowing before you rename anything:

* **`pms_app` is not an arbitrary name.** It is hard-coded as `APP_ROLE` in
  `alembic/versions/0002_row_level_security.py`, which grants that role its CRUD rights and revokes
  `CREATE` on the schema — conditionally, only if a role by that name exists. Use a different name
  and the migration silently skips the grants.
* **`pms_owner` does not get a free pass on tenant data.** The tenant-owned tables are
  `FORCE ROW LEVEL SECURITY`, so the policies apply to the owner as well. That is why the seeder
  binds each clinic with `set_tenant` before writing its rows (`app/seed/loader.py`) rather than
  writing them all in one unbound pass. Ownership buys the right to change the *shape* of the
  database, not the right to ignore the boundary.

### Creating the database and roles from scratch

For the docker path this is already done for you — `scripts/bootstrap_db.sql` is mounted into
`/docker-entrypoint-initdb.d/` and runs on the first boot of an empty volume. The steps below are
for a PostgreSQL you already run. Every command is executable as written; replace the two
passwords.

**1. Connect as the PostgreSQL administrator.**

```bash
psql -h localhost -p 5432 -U postgres
```

**2. Create the database.**

```sql
CREATE DATABASE pms_api;
```

**3. Create `pms_owner` — the privileged migration role.**

```sql
CREATE ROLE pms_owner LOGIN PASSWORD 'owner-password-change-me' NOBYPASSRLS;
```

**4. Create `pms_app` — the restricted runtime role.**

```sql
CREATE ROLE pms_app LOGIN PASSWORD 'app-password-change-me'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
```

`NOBYPASSRLS` is the one attribute that must never be relaxed. A single `ALTER ROLE pms_app
BYPASSRLS` undoes every isolation guarantee in this project.

**5. Make `pms_owner` the owner of the database.**

```sql
ALTER DATABASE pms_api OWNER TO pms_owner;
```

(`scripts/bootstrap_db.sql` folds steps 2 and 5 into one `CREATE DATABASE pms OWNER pms_owner`.)

**6. Configure the `public` schema.** Reconnect to the new database first — the remaining steps
are all inside it:

```bash
psql -h localhost -p 5432 -U postgres -d pms_api
```

```sql
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO pms_owner;
GRANT USAGE ON SCHEMA public TO pms_app;
-- The application never creates objects.
REVOKE CREATE ON SCHEMA public FROM pms_app;
```

**7. Grant `pms_app` CRUD on existing tables.**

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pms_app;
```

On a fresh database there are no tables yet and this is a no-op — that is expected. It matters
when you add the role to a database that has already been migrated.

**8. Configure default privileges, so tables Alembic creates later are granted automatically.**

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE pms_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pms_app;
```

**This is the step that is easy to miss and expensive to skip.** Without it, migrations succeed,
the tables exist, and then every single request fails with *permission denied* — because the grant
in step 7 only covered tables that existed at the time.

**9. Grant sequence permissions.**

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE pms_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO pms_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pms_app;
```

Then run the migrations and the seed as the owner:

```bash
make migrate    # alembic upgrade head, as pms_owner
make seed
```

`alembic/versions/0002_row_level_security.py` re-applies the step 7 and 9 grants itself, revokes
`CREATE` on the schema, and additionally revokes `UPDATE` and `DELETE` on `audit_events` — the
audit trail is written, never rewritten. Doing it by hand first means a partially migrated database
is never briefly permissive.

### Verifying the setup

**10. Neither role may bypass row-level security.**

```sql
SELECT rolname, rolbypassrls, rolsuper FROM pg_roles WHERE rolname LIKE 'pms%';
```

```text
  rolname  | rolbypassrls | rolsuper
-----------+--------------+----------
 pms_owner | f            | f
 pms_app   | f            | f
```

Both `rolbypassrls` columns must read `f`. Anything else and PostgreSQL is no longer enforcing
anything, whatever the application code does.

**11. The database owner is `pms_owner`.**

```sql
SELECT datname, pg_get_userbyid(datdba) AS owner
FROM pg_database WHERE datname = 'pms_api';
```

```text
 datname |   owner
---------+-----------
 pms_api | pms_owner
```

**12. `pms_app` can do application work — and nothing else.** Connect as the application role:

```bash
psql -h localhost -p 5432 -U pms_app -d pms_api
```

Reads and writes work, scoped to the bound clinic:

```sql
-- Unbound: the failure mode is emptiness, never leakage.
SELECT count(*) FROM patients;                        -- 0

-- Bound to one clinic, inside a transaction:
BEGIN;
SELECT set_config('app.tenant_id', (SELECT id::text FROM tenants LIMIT 1), true);
SELECT count(*) FROM patients;                        -- that clinic's patients
COMMIT;
```

Structural changes must all be refused:

```sql
CREATE TABLE nope (id int);
-- ERROR:  permission denied for schema public

ALTER TABLE patients DISABLE ROW LEVEL SECURITY;
-- ERROR:  must be owner of table patients

DROP POLICY tenant_isolation ON patients;
-- ERROR:  must be owner of relation patients
```

And history cannot be edited — the rules in migration `0002` turn both into no-ops:

```sql
UPDATE audit_events SET action = 'x';   -- UPDATE 0
DELETE FROM audit_events;               -- DELETE 0
```

If any of those three `ERROR` lines does not appear, the runtime role is over-privileged. Fix that
before going further; nothing above it in the stack compensates for it.

### Migration and runtime flow

```text
FastAPI
  ↓
DATABASE_URL
  ↓
pms_app
  ↓
PostgreSQL
  ↓
CRUD + RLS


Alembic / Seeder
  ↓
MIGRATION_DATABASE_URL
  ↓
pms_owner
  ↓
PostgreSQL
  ↓
Schema / migrations / RLS policies
```

Two paths, two credentials, one database. The left-hand path runs thousands of times a second and
can only move rows around. The right-hand path runs once per deploy and can reshape everything.

### Why the roles are separated

The whole isolation design rests on one assumption: **the worst an attacker who controls the API
process can do is what `pms_app` can do.**

If the API process is compromised, the attacker should obtain `pms_app` credentials and nothing
else. Because `pms_app` does not own the tables, cannot bypass RLS, and cannot modify RLS policies
or the schema, **the database remains the final tenant-isolation boundary** — it holds even when
every layer above it is in the attacker's hands. The blast radius is one clinic's rows, which is
the point.

Hand that same process the `pms_owner` credential and the assumption collapses. Any leak of the
environment — a dependency exploit, an SSRF that reads `/proc/self/environ`, a crash dump, a debug
page that prints settings, a `docker inspect` by a lower-privileged operator — yields a role that
owns the tables and can run:

```sql
ALTER TABLE patients DISABLE ROW LEVEL SECURITY;
```

One statement, and the last layer is gone. The three above it are application code the attacker
already controls.

So: **do not put `MIGRATION_DATABASE_URL` in the API container in production.** Run migrations as
a separate job or init container that holds the `pms_owner` credential, applies `alembic upgrade
head`, and exits. The API container gets `DATABASE_URL` only. It is also the natural fit for the
lifecycle — migrations run once per deploy, not once per request.

`docker-compose.yml` deliberately does the opposite: one container holds both URLs and runs
`alembic upgrade head && python scripts/seed.py && uvicorn …`. That is correct for local
development, where the passwords are `-change-me` literals and one command beats three. It is not
the shape to copy into production.

---

## Endpoints

Everything is under `/v1`. The full list is at `/docs`; this is the shape of it.

| Area | Routes |
|---|---|
| **Auth** | `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`, `POST /platform/auth/login` |
| **Tenant** (§3.2, §3.3) | `GET /tenant`, `GET|PATCH /tenant/config`, `GET /tenant/features`, `POST /tenant/features/{key}/purchase`, `.../deactivate`, `GET|POST /tenant/fields`, `DELETE /tenant/fields/{id}`, `GET /tenant/schema/{entity}` |
| **Staff** | `GET /users`, `GET /users/doctors`, `POST /users`, `PATCH /users/{id}/status`, `GET /users/permissions` |
| **Me** | `GET /auth/me`, `PATCH /auth/me/preferences` |
| **Patients** (§3.4, §3.13) | `GET /patients` (paged), `POST /patients`, `GET|PATCH /patients/{id}`, `GET /patients/{id}/history` |
| **Scheduling** (§3.5, §3.6) | `GET|POST /appointments`, `GET /appointments/availability`, `PATCH /appointments/{id}/status`, `POST /appointments/{id}/checkin`, `GET /queue`, `POST /queue/walkin`, `PATCH /queue/{id}/status`, `GET /queue/display` |
| **Consultation** (§3.7, §3.8) | `GET /encounters/schema`, `GET|POST /encounters`, `GET|PATCH /encounters/{id}`, `POST /encounters/{id}/prescriptions`, `GET /prescriptions` |
| **Pharmacy** (§3.9) 🔒 | `GET /pharmacy/prescriptions`, `POST /pharmacy/prescriptions/{id}/dispense` |
| **Lab** (§3.10) 🔒 | `POST /encounters/{id}/lab-orders`, `GET /encounters/{id}/lab-results`, `GET /lab/orders`, `POST /lab/orders/{id}/start`, `POST /lab/orders/{id}/results`, `PATCH /lab/orders/{id}`, `GET|POST /lab/orders/{id}/attachments`, `GET|DELETE /lab/orders/{id}/attachments/{aid}` |
| **Inventory** (§3.11) 🔒 | `GET|POST /inventory/items`, `PATCH /inventory/items/{id}`, `/batches`, `/levels`, `/expiring`, `GET|POST /inventory/transactions` |
| **Billing** (§3.12) 🔒 | `GET|POST /billing/invoices` (`?department=clinic\|lab\|pharmacy`), `GET /billing/invoices/suggestions`, `GET /billing/invoices/{id}`, `POST .../issue`, `.../void`, `.../payments` |
| **Pricing** (§3.12) | `GET|POST /pricing/services`, `PATCH|DELETE /pricing/services/{id}`, `POST /pricing/services/import-lab-catalogue` |
| **Accounts** (§3.12) 🔒 | `GET /billing/patients/{id}/account`, `GET /billing/patients/{id}/charges`, `POST /billing/patients/{id}/invoices`, `POST /billing/patients/{id}/payments` |
| **Realtime** (§3.6) | `WS /ws/queue?access_token=…` |
| **Analytics** (§3.14) | `GET /analytics/dashboard` |
| **Platform** (§3.1) | `GET|POST /platform/tenants`, `PATCH /platform/tenants/{id}/status`, `GET /platform/snapshot`, `GET /platform/me` |
| **Public** | `GET /v1/public/tenant`, `GET /health`, `GET /health/db` |

🔒 = behind an entitlement. A clinic that has not bought the module gets `403 feature_not_enabled`,
whatever its staff's roles say.

---

### Platform billing

```
GET    /v1/settings/billing                             the whole billing screen, in one response
GET    /v1/settings/billing/plans                       the base plans on offer
POST   /v1/settings/billing/subscribe                   take a plan (plan code only — never a price)
POST   /v1/settings/billing/plan                        change plan; credits the unused remainder
POST   /v1/settings/billing/pause | /resume             stop and restart billing
POST   /v1/settings/billing/cancel                      at the end of the period already paid for
POST   /v1/settings/billing/resume-cancellation         change your mind; nothing is charged
GET    /v1/settings/billing/account                     who to bill, where, under which tax identity
PATCH  /v1/settings/billing/account
POST   /v1/settings/billing/addons/{feature_key}        switch a module on (full or prorated)
DELETE /v1/settings/billing/addons/{feature_key}        schedule it to stop at the cycle end
GET    /v1/settings/billing/invoices                    billing history
GET    /v1/settings/billing/invoices/{id}
GET    /v1/settings/billing/invoices/{id}/download      the PDF, from the stored line items
POST   /v1/settings/billing/invoices/{id}/checkout      Pay Now — returns the public key and an order
POST   /v1/settings/billing/invoices/{id}/verify        the claim, checked three ways server-side
GET    /v1/settings/billing/payments

GET    /v1/platform/billing/metrics                     MRR, revenue, subscriptions, add-ons — filtered
GET    /v1/platform/billing/plans
GET    /v1/platform/billing/tenants/{id}/invoices
POST   /v1/platform/billing/tenants/{id}/refunds        nothing refunds automatically
POST   /v1/platform/billing/tenants/{id}/invoices/{id}/void
GET    /v1/platform/billing/refunds
GET    /v1/platform/billing/webhooks                    the delivery log, refusals included

POST   /v1/webhooks/{gateway}                           unauthenticated; the signature is the auth
```

## One person's own settings

`PATCH /v1/auth/me/preferences` stores interface state against the account — which guided tour
they have finished, and anything of that kind later. Three things keep it small:

* **There is no user id in the route.** The only account it can touch is the one that made the
  request, so there is nothing to point at someone else.
* **The keys are a closed set.** Anything the schema does not know is refused, and what is stored
  is the validated shape — so a staff record cannot become a bucket of arbitrary client state.
* **It is not the clinic's configuration.** `tenant_configs` is how a practice has set the product
  up (§3.3); this is how one person has set their own view of it, and it travels with them rather
  than with the browser or the clinic.

## Reading at scale

Two endpoints exist so that a browser never has to hold what it is only going to
summarise or scroll:

**`GET /v1/patients` is a page, not a collection.** It takes `search`, `limit`,
`offset`, `sort`, `order` and the register's filters (`seen_on`,
`registered_since`, `with_allergies`), and returns `{items, total, limit,
offset}`. Searching, sorting and paging all happen in PostgreSQL under this
clinic's policy, and the count runs under the same filter as the page — so a
total can never describe rows the caller may not read. `sort` names one of a
fixed set of columns; an ORDER BY is a query plan, not a free-text field.

**Rows that mention a patient carry the name.** Appointments, queue entries,
encounters, prescriptions, lab orders and invoices all come back with
`patient_name` and `patient_mrn` on them, loaded in one extra query per page.
Without that, every screen showing a list would need the whole register just to
render names — which is exactly what makes registers get loaded whole.

**`GET /v1/analytics/dashboard` returns figures, not rows.** Everything the
dashboard and the reports page show is aggregated in the database: the trailing
series, the queue funnel, ranked diagnoses, the money, the cohort breakdowns and
the clinical-type panels counted straight out of `clinical_ext` with JSONB
operators. A clinic with fifty thousand patients sends the same few kilobytes as
one with fifty, and — because there is one implementation of each figure — the
reports cannot drift from the ledger.

## Rules the API keeps

Things that are enforced here rather than hoped for in the client:

* **State machines are closed.** Appointment, queue entry, lab order and invoice transitions are
  declared in `app/models/enums.py`; anything not listed is a 409, whoever asks.
* **Stock is the ledger.** On-hand is `sum(qty)` over `inventory_transactions`, never a stored
  counter. Dispensing writes the ledger row and updates the prescription item in one transaction,
  under a row lock, and cannot take more than a batch holds.
* **Money is derived.** Invoice totals come from the line items and payments every time, so a
  status cannot disagree with the arithmetic. Overpaying is refused; a void invoice takes nothing.
* **Prices are the clinic's, not the code's.** What a consultation, a procedure or a particular
  scan costs lives in `service_prices`, one row per tenant per code, and billing reads it through
  `app/services/pricing.py`. A line with no tariff behind it is suggested at zero and says so
  (`priced: false`) rather than quoting a number nobody agreed to. A stock line's selling price is
  the same idea: `unit_price` is always the answer, and `pricing_mode` records whether it was typed,
  taken as a margin over cost, or read off the MRP — so changing the cost of a line priced on margin
  moves the price with it.
* **Each counter bills its own.** `invoices.department` says which desk raised an invoice. It is
  derived from the line items when it is not sent — an invoice of nothing but lab lines is the
  lab's — so the lab and the pharmacy chase their own money without the clinic's ledger being
  filtered by description.
* **A charge is billed once.** `invoice_line_items.ref_id` names the row a line bills for: a
  prescription item, a lab order, an encounter. `app/services/accounts.py` works out what a patient
  has had that nothing points at yet, which is what lets the dispensing window ask for money for
  what it just handed over without the front desk charging for it again on the way out. Voiding an
  invoice puts its charges back, because voiding is how a mistaken charge is undone.
* **Money is applied, not allocated by hand.** A payment at one counter clears that counter's
  invoices oldest first, and never touches another's — a patient paying for their medicine has not
  paid for their scan. Paying more than a counter is owed is refused rather than left as a credit
  nobody can see.
* **The final settlement is a subtraction nobody performs.** `GET /billing/patients/{id}/account`
  answers the question asked at the door — what is still owed, counter by counter, plus a
  `total_due`. Money already taken at a window is not deducted from that total; it was never in it,
  because a settled counter has no `due`. Each counter also reports the `paid` it has taken over
  every invoice it ever raised, so a pharmacy that collected at the hatch reads as *paid* rather
  than disappearing and looking like a pharmacy that never charged anything. Settling everything is
  the same four routes run per counter, which is why the ledger still shows three separate bills
  afterwards.
* **The counter takes the money.** Every clinical role carries `billing:read` and `billing:write`:
  the pharmacist asks as the medicine is handed over, the lab tech as the report is collected, the
  clinician for the consultation. Which counter is being settled is a field on the request, not a
  role, so the ledger keeps them apart without the permission table having to.
* **Lab results are withheld until verified.** Ordering, resulting and verifying are separate
  steps, and `GET /encounters/{id}/lab-results` returns verified orders only unless asked
  otherwise — and even then the values themselves stay redacted. `resulted_by` and `verified_by`
  are both recorded, so a four-eyes rule can be switched on in `app/services/lab.py` without a
  migration.
* **Sequences do not collide.** MRNs, invoice numbers and daily queue tokens are minted under a
  lock on the clinic's configuration row, so two front desks cannot take the same one.
* **Signed notes are immutable.** A signed consultation cannot be edited.
* **The public display shows a first name and an initial.** It is a screen in a waiting room.
* **Everything is written down.** `audit_events` records every state change and every
  authentication decision, and the application role has no `UPDATE` or `DELETE` on it — plus a
  rule in the database that turns both into no-ops.

## The queue socket (§3.6)

`WS /v1/ws/queue` pushes one thing: *something in this clinic's queue moved*. Clients re-read
`GET /v1/queue` when they hear it, so the socket never becomes a second, divergent API and the
guards on that endpoint are the only guards that matter.

* **It is authorised exactly like a request.** The token names a clinic, the address names a
  clinic, and they have to agree; the role needs `queue:read`. A browser cannot set headers on a
  socket, so the token travels as `?access_token=` — short-lived, and the socket closes itself
  with **4440** when it expires, which is the client's cue to refresh and reconnect. Other close
  codes: **4401** not your clinic, **4403** not your role.
* **Events carry no patient data** — an entry id, a token number, a status, a date. Names,
  reasons and clinical detail stay behind the HTTP endpoints, where the permissions are.
* **They are published after the transaction commits**, as background tasks, so nothing is
  announced that could still be rolled back.
* **Channels are per clinic**, and a subscriber is handed its channel rather than choosing one.
  A client that cannot keep up is dropped rather than slowing the request that published; it
  resynchronises when it reconnects, because reconnecting means re-reading.
* `app/core/events.py` is an in-process broker. Behind more than one worker, swap `publish` for a
  Redis or NATS publish and have each worker subscribe — the interface is the same shape.

---

## Other security properties

* **Passwords** — Argon2id (`argon2-cffi`), rehashed on sign-in when parameters change. An
  unknown address costs the same time as a wrong password, because a dummy verification runs
  either way, and both return the same sentence.
* **Tokens** — 15-minute access JWTs; refresh tokens are opaque random strings stored only as
  SHA-256, set as `HttpOnly; SameSite=Lax` cookies scoped to `/v1/auth`, and rotated on every
  use. Presenting a spent one revokes the whole sign-in chain, on the assumption it was stolen.
* **Rate limits** — a tight per-IP-and-email limit on sign-in, a blunt one on everything else.
  In-process, so put a shared limiter in front of a multi-worker deployment.
* **Input** — every request model forbids unknown fields and bounds every string; `clinical_ext`
  is validated against the clinic's own resolved schema and unknown keys are dropped rather than
  stored, so a clinical record cannot be used as free storage.
* **Output** — no password material ever leaves the API, and errors carry a code, a sentence, and
  a request id, never an exception.
* **Headers and CORS** — `nosniff`, `DENY`, `no-referrer`, a locked-down CSP, HSTS when cookies
  are secure; CORS is limited by regex to the apex and its subdomains, with credentials allowed.
* **Startup refuses bad configuration** — a production process will not boot with a default JWT
  secret, `DEBUG` on, insecure cookies, the owner role in `DATABASE_URL`, or a role that bypasses RLS.

---

## Tests

```bash
make test
```

Against a real PostgreSQL with the real migrations — row-level security does not exist in
SQLite, and it is the thing most worth testing.

`tests/test_isolation.py` is the file to read first. It attacks the boundary from every angle:
a valid token at another clinic's address, a real id from another clinic on every route, a
`tenant_id` planted in a request body, custom fields, staff lists — and then drops to the
database with the application's own credentials to confirm that an unbound session sees zero
rows, that a `WHERE tenant_id = <someone else>` returns nothing, that the app role cannot
disable a policy, and that it does not hold `BYPASSRLS`.

The rest: `test_auth.py` (enumeration, lockout, rotation, reuse detection, cross-host tokens),
`test_guards.py` (entitlements and roles, suspension, self-lockout), `test_workflows.py` (the
whole loop from registration to a paid invoice, plus the state machines and the ledger),
`test_schema_and_platform.py` (field resolution, clinic creation, and the checks that keep the
isolation machinery honest), and `test_realtime.py` (the socket: refused at another clinic's
address, one clinic's events never reaching another, and no patient data on the wire).

Platform billing has eight files of its own, and they split by what can only be tested one way.
The date-driven behaviour is tested against the services with an explicit `today` — there is no
other way to bill the same clinic through a 28-day February, a 29-day one and a 31-day January
without waiting a year — while the surface, the shapes and the authorisation are tested over
HTTP:

* `test_billing_proration.py` — the month arithmetic, over 28/29/30/31-day cycles, including the
  test that fails if anybody reintroduces a `/ 30`;
* `test_billing_addons.py` — requirements 5 to 8, one test each at minimum, plus the
  double-charge a mid-cycle activation followed by a renewal would otherwise cause;
* `test_billing_subscription.py` — the lifecycle, idempotent renewal, catching a clinic up one
  real month at a time, and plan changes;
* `test_billing_invoices.py` — that the stored totals equal the sum of the stored lines, that
  tax follows the `tax_rates` table rather than the code, and that the immutability trigger and
  rules actually hold when attacked through the ORM;
* `test_billing_payments.py` — the three-step verification, with a stubbed gateway and a **real**
  HMAC: a correctly signed ₹1 payment for a ₹2,999 invoice must be refused;
* `test_billing_webhooks.py` — signatures, storage, redelivery, and a refused signature being
  kept and audited;
* `test_billing_metrics.py`, `test_billing_jobs.py` — the figures, and the jobs run twice;
* `test_billing_architecture.py` — the gateway-independence rule, enforced rather than reviewed.

These tests never touch the seeded demo clinics: each creates its own, because the rest of the
suite asserts on Northgate's entitlements and Rowan's, and a billing test that subscribed one of
them to a plan bundling the lab would break tests in three other files.

---

## Layout

```
app/
  main.py              wiring: middleware, error handlers, routes
  core/
    config.py          settings, and what production refuses to start without
    security.py        Argon2, JWTs, refresh-token hashing
    tenancy.py         Host header → clinic (mirrors the web app's tenancy.ts)
    permissions.py     roles → permissions
    errors.py          one error vocabulary; 404 for another clinic's rows
    rate_limit.py      fixed-window limiter
    logging.py         request context, security headers
  db/
    base.py            Base, TimestampMixin, TenantScoped
    session.py         engine, one transaction per request, app.tenant_id
    guard.py           the ORM isolation listeners
  models/              tables — clinical, identity, ops, tenancy, billing, enums
  schemas/             request and response contracts
  services/            the decisions: auth, tenants, patients, scheduling,
                       encounters, pharmacy, lab, inventory, billing, history,
                       analytics, platform, audit
    payment/           platform billing — what a clinic pays *us*
      gateway.py       the seam: what a payment gateway is, without naming one
      money.py         Decimal rounding, and calendars that count real days
      billing_service.py       plans, add-on prices, cycles, the billing screen
      subscription_service.py  create, pause, resume, cancel, renew, change plan
      payment_service.py       orders, server-side verification, refunds
      invoice/         building the monthly invoice, once
      proration/       charging for part of a cycle, over its real length
      addon/           switching modules on and off, and what that costs
      tax/             tax as configuration — no jurisdiction is in the code
      webhook/         receiving and processing callbacks, exactly once each
      metrics/         the console's billing figures
      razorpay/        the only code that knows what Razorpay is
  jobs/                the six billing jobs, and the worker loop
  api/
    deps.py            the §4 middleware stack as dependencies
    v1/                routers
  seed/                the demo dataset and its loader
alembic/versions/
  0001_initial_schema.py
  0002_row_level_security.py    policies, grants, and the audit-log rules
  0009_platform_billing.py      subscriptions, invoices, payments, webhooks
scripts/
  bootstrap_db.sql     the two roles, and why they are two
  seed.py
  worker.py            the patient-messaging outbox
  billing_worker.py    the billing jobs, on a loop or one by name
```

---

## Platform billing (Settings → Billing)

Two ledgers live in this codebase and they never touch. `app/services/billing.py` is a **clinic
billing its patients** — a consultation, a scan, some medicine, settled at the counter.
`app/services/payment/` is **the platform billing a clinic** for the software. Different money,
different people, different tables: the subscription ledger's tables carry a `subscription_`
prefix because `invoices` and `payments` were already taken by the other one.

### The shape

Every clinic has one mandatory monthly **base plan** (`plans`), and modules are monthly
**add-ons** priced by `plan_features` with a fallback to `features.price_monthly` — so the common
case needs no row at all and a plan that bundles or discounts a module needs exactly one.
Entitlement is still `tenant_features`, the same row the feature guard reads: "can this clinic use
the lab" and "is this clinic paying for the lab" have to be one answer, so that row gained three
billing columns rather than a second table growing up beside it.

A **billing cycle** is half-open, `[period_start, period_end)`, and `period_end` is the first day
of the next one. That is what makes proration honest — `period_end - period_start` is 28, 29, 30
or 31, and nothing in the package divides by 30.

### What the money does

| Situation | What happens |
| --- | --- |
| Add-on switched on, first day of the cycle | Full monthly charge — and not as a special case: proration from `period_start` *is* the whole month |
| Add-on switched on mid-cycle | Prorated over the days the cycle actually has, invoiced immediately |
| Add-on cancelled | Stays usable to the end of the period already paid for. No refund, no credit, nothing on the next invoice |
| Add-on switched back on before that | The scheduled cancellation is dropped. **Nothing is charged** — it never stopped |
| Plan changed mid-cycle | The old cycle ends today and its unused remainder becomes a credit line on the new cycle's invoice |
| Invoice unpaid past its grace period | The subscription is flagged `past_due`. Nothing is switched off — suspension is a person's decision, not a cron job's |

An invoice is `Base plan + Add-ons + Proration − Discounts/Credits + Taxes`, and it is **immutable
once issued**: a trigger refuses any change to its number, currency, period or money columns, and
`subscription_invoice_items` has `DO INSTEAD NOTHING` rules against UPDATE and DELETE — the same
treatment `audit_events` gets. A mistake is corrected by voiding and re-raising, never by editing.

Tax is rows in `tax_rates`, not code. Two 9% `intra_state` rows produce two tax lines; one 18%
`inter_state` row produces one; no rows produce a zero tax total. Nothing in the package knows
what GST is.

### Gateway independence

`app/services/payment/gateway.py` defines what a payment gateway *is* — protocols and DTOs,
amounts as `Decimal`, two failure shapes (`GatewayUnavailable` retries, `GatewayRefused` does
not). Everything that decides money imports that and nothing else; everything that talks HTTP
lives in `payment/razorpay/` and decides nothing. Adding Cashfree or Stripe is a new package
beside `razorpay/` and one line in `_ADAPTERS`.

`tests/test_billing_architecture.py` enforces it rather than trusting review: it parses the core
modules for gateway imports and gateway-named attributes, blocks the adapter from `sys.meta_path`
and imports every core service anyway, and asserts `httpx` appears in exactly one file under
`payment/`.

The PMS is the source of truth throughout. A gateway is told what happened and asked to collect;
it is never asked what a clinic owes. A `subscription.cancelled` webhook is recorded in
`gateway_status` and **not obeyed** — a gateway that thinks a subscription is halted does not get
to stop a clinic whose invoices this system knows are paid.

### Never trusting the browser

What Checkout hands back is a claim, and it is checked three ways, all server-side:

1. the signature, against the key secret — proving the fields were not edited in the browser;
2. a re-fetch of the payment from the gateway — proving it exists and was captured;
3. the amount and currency against the invoice *this* system raised.

A correctly signed ₹1 payment for a ₹2,999 invoice fails at step three, which is what step three
is for. No request body anywhere carries an amount: `SubscribeIn` takes a plan code,
`PaymentVerifyIn` takes three opaque ids, and `InputModel` rejects unknown keys — so a client
cannot smuggle a price into a call that decides money.

Webhooks are stored before they are believed, including the ones whose signature fails: those are
kept as `rejected`, never processed, and written to the audit trail. Redelivery is a no-op because
of `uq_webhook_event_id`, not because anything counted, and the endpoint answers `200 received`
either way — telling an unauthenticated caller that their forgery was detected helps them iterate.

### Duplicates

Five indexes, not five checks in Python. Every job is idempotent because of these:

| Index | What it prevents |
| --- | --- |
| `uq_subscriptions_one_live_per_tenant` | Two subscriptions for one clinic |
| `uq_billing_cycle_period` | The same period opened twice |
| `uq_subscription_invoice_per_cycle` (partial, `status <> 'void'`) | A cycle invoiced twice |
| `uq_subscription_payment_gateway_id` | One gateway payment booked twice — or to two clinics |
| `uq_webhook_event_id` | One delivery processed twice |

Invoice numbers come from a PostgreSQL sequence rather than a locked row, so two renewal runs get
two numbers without waiting for each other and no number is ever reused.

### Jobs

`make billing` runs the loop; `python scripts/billing_worker.py <JobName>` runs one by hand.

```
GenerateMonthlyInvoicesJob      backstop: raise any cycle's missing invoice
ProcessBillingJob               apply cancellations, flag past-due, clear it again
ProcessSubscriptionRenewalsJob  close the cycle, apply what was scheduled, open the next
SendInvoiceEmailJob             email each new invoice with a Pay Now link
ProcessRazorpayWebhookJob       work through the stored deliveries
SyncRazorpaySubscriptionJob     reconcile with the gateway, without obeying it
```

Each visits one clinic at a time, in its own transaction, with `app.tenant_id` bound — so a cron
job goes through row-level security exactly as a request does, and one clinic's failure is one
clinic's failure rather than a month of missing invoices. "Today" is the clinic's own date in the
clinic's own timezone: a clinic in Kolkata and one in London do not renew at the same instant.

### Authorisation

`SUBSCRIPTION_READ` and `SUBSCRIPTION_MANAGE` belong to the tenant administrator alone — the
narrowest permissions in the table. Every clinical role can take a *patient's* money; only the
person who runs the practice can change what the practice itself pays for. The console's metrics
are on `admin.<apex>` behind `current_platform_user`, and they are gathered one clinic at a time
with that clinic bound: there is no `BYPASSRLS` role in this application and the console does not
get one either.

---

## Deploying

* Run migrations as `pms_owner` and the API as `pms_app`. Never give `pms_app` `BYPASSRLS`,
  ownership of the tables, or `CREATE` on the schema. Give the API container `DATABASE_URL` only —
  `MIGRATION_DATABASE_URL` belongs to the migration job. See
  [The database](#the-database-one-database-two-roles).
* Terminate TLS in front, set `COOKIE_SECURE=true`, and set `COOKIE_DOMAIN=.your-apex` so a
  refresh cookie is shared across clinic subdomains.
* Route `<slug>.your-apex` to the API with the `Host` header intact — that header is how a
  request names its clinic. Run uvicorn with `--proxy-headers` so the client IP behind the
  load balancer is the one that gets rate-limited and audited, and allow the `Upgrade` header
  through for `/v1/ws/*`.
* Set `REFRESH_COOKIE_PATH` to the path the *browser* uses for the refresh call. Served behind a
  proxy that mounts this API at `/api`, that is `/api/v1/auth` — not `/v1/auth`. The default `/`
  works everywhere; narrowing it is worth doing when the prefix is fixed.
* `ALLOW_TENANT_HEADER` is a development convenience for proxies that rewrite `Host`; production
  refuses to start with it on.
* `GET /health` for liveness, `GET /health/db` for readiness. `/docs` and `/openapi.json` are
  served outside production only.

## Notes and limits

* The rate limiter and the event broker are both per process. Behind more than one worker, put a
  shared limiter in front and swap the broker for Redis (see above).
* Analytics reads the transactional tables directly, as §3.14 prescribes. When a clinic's
  history outgrows that, cache the aggregations rather than adding a second store.
* Feature purchase records the outcome of a payment; the payment itself belongs to whatever
  processor the platform uses.
