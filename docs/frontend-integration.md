# Wiring the web app to this API

**This is done.** `../pms-multi-tenant-web` now runs against this API: there is no mock database
in it any more, and its `src/lib/api/index.ts` is a set of `fetch` calls against the endpoints
below. This document is both the record of how it was wired and the reference for doing it again
from another client.

The seam held: every function in that file was already async and named after an endpoint, so
replacing each body was the whole integration and nothing above it had to move.

## 1. Where the API lives

The clinic is decided by the `Host` header, so the browser must reach the API on a host that
carries the slug. Two ways:

**Same host, different path** — what the web app does. The API sits behind `/api` on each
clinic's subdomain, so `northgate.astrale.health/api/v1/patients` arrives with `Host:
northgate.astrale.health` intact. In development, `next.config.ts` rewrites it:

```ts
async rewrites() {
  return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/:path*" }];
}
```

Next rewrites the `Host` header to the destination, so for local development set
`ALLOW_TENANT_HEADER=true` on the API and send the slug explicitly:

```ts
const headers = { "X-Tenant-Slug": host.slug ?? "" };   // dev only
```

The header is refused outright in production; there, the real `Host` does the job.

Two things to get right either way, both of which cost an afternoon if you miss them:

* **The refresh cookie's path must match the path the browser uses.** Behind the `/api` rewrite
  the browser's refresh URL is `/api/v1/auth/refresh`, so a cookie scoped to `/v1/auth` is never
  sent and every reload silently signs the user out. Set `REFRESH_COOKIE_PATH` to match, or leave
  it at `/`.
* **Host-routing middleware must not touch `/api/*`.** The web app's `src/proxy.ts` redirects the
  apex to `/platform`; until it was taught to skip `/api`, the console's sign-in POST was
  answered with a redirect to an HTML page, which surfaces as "the console could not be reached"
  rather than as anything resembling a routing problem.

**Separate host.** Serve the API at `<slug>.api.astrale.health` and call it cross-origin with
`credentials: "include"`. CORS already allows the apex and its subdomains with credentials.

## 2. Auth

```ts
const res = await fetch("/api/v1/auth/login", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  credentials: "include",                 // the refresh cookie
  body: JSON.stringify({ email, password }),
});
const { access_token, user, permissions } = await res.json();
```

Keep `access_token` in memory (not `localStorage`) and send it as `Authorization: Bearer`.
On a 401, call `POST /api/v1/auth/refresh` with `credentials: "include"` once, then retry;
if that fails too, drop to the sign-in screen. `signOut` becomes `POST /api/v1/auth/logout`.

The tenant no longer comes from a client-side session — it comes from the address and the
token. `session.tenantId` becomes the `tenant_id` the login response returns, and switching
tenant in the topbar becomes a link to the other clinic's subdomain, which it already is.

## 3. Function-by-function

| `src/lib/api` | Endpoint |
|---|---|
| `authenticate` | `POST /v1/auth/login` |
| `listTenants` (console) | `GET /v1/platform/tenants` |
| `platformSnapshot` | `GET /v1/platform/snapshot` |
| `getTenant` | `GET /v1/public/tenant` (sign-in screen), `GET /v1/tenant` (inside) |
| `listUsers` / `listDoctors` | `GET /v1/users`, `GET /v1/users/doctors` |
| `createUser` | `POST /v1/users` — omit `password` to create an `invited` account |
| `setUserPassword` | `POST /v1/users/{id}/password` — an admin setting a colleague's; the first one accepts an invitation, and none of them unlock a locked account |
| `setUserStatus` | `PATCH /v1/users/{id}/status` |
| `savePreferences` | `PATCH /v1/auth/me/preferences` — the signed-in person's own interface state |
| `listFeatures` / `purchaseFeature` / `deactivateFeature` | **removed, no replacement** — every clinic has every module. `GET /v1/tenant/features`, `/tenant/feature-catalog`, `/tenant/features/{key}/purchase`, `.../deactivate` and `/catalog/features` answer `404`/`405` |
| `getSubscription` | `GET /v1/tenant/subscription` — `plan` is `{price_monthly, billed, not_billed_reason, monthly_total}`: one flat clinic fee, no per-module list; bill `lines` are `{name, amount}` |
| `createSupportRequest` | `POST /v1/tenant/support-requests` — `kind` is `walkthrough` or `general` (no `activation`, no `feature_key`) |
| `getConfig` / `updateConfig` | `GET` / `PATCH /v1/tenant/config` |
| `listFieldDefinitions` | `GET /v1/tenant/fields` |
| `getSchema` | `GET /v1/tenant/schema/{entity}` — the clinical type is the server's, so the argument goes away |
| `addFieldDefinition` / `removeFieldDefinition` | `POST /v1/tenant/fields` / `DELETE /v1/tenant/fields/{id}` |
| `listPatients` | `GET /v1/patients?search=&limit=&offset=&sort=&order=` — returns `{items, total, limit, offset}` |
| `getPatient` / `createPatient` / `updatePatient` | `GET|POST /v1/patients`, `PATCH /v1/patients/{id}` |
| `getPatientHistory` | `GET /v1/patients/{id}/history` |
| `listPatientNotes` / `addPatientNote` | `GET|POST /v1/patients/{id}/notes` — read under `encounter:read`, written under `encounter:write`; no edit or delete, a correction sends `amends_id` (its author only) |
| `listAppointments` / `createAppointment` | `GET|POST /v1/appointments` — paged; also `date=`, `doctor_id=`, `search=` |
| `conflictsFor` | `GET /v1/appointments/availability` — now async |
| `setAppointmentStatus` | `PATCH /v1/appointments/{id}/status` |
| `checkIn` | `POST /v1/appointments/{id}/checkin` |
| `listFollowUps` | `GET /v1/follow-ups?days=&date=&status=&doctor_id=&search=&limit=&offset=` — the call list; `{items, total, counts, today}`, counts taken before `date`/`status` |
| `listFollowUpCalls` / `recordFollowUpCall` | `GET|POST /v1/follow-ups/{appointment_id}/calls` — a `rescheduled` call carries `new_slot` and is checked against the diary under a lock (409 if taken, 422 if past or outside the clinic's day); `declined` may set `cancel_appointment` |
| `listQueue` / `addWalkIn` / `setQueueStatus` | `GET /v1/queue`, `POST /v1/queue/walkin`, `PATCH /v1/queue/{id}/status` |
| `listEncounters` / `getEncounter` | `GET /v1/encounters` (paged; `search=`, `patient_id=`), `GET /v1/encounters/{id}` |
| `createEncounter` | `POST /v1/encounters` — `sign` is a field on the body |
| `updateEncounter` | `PATCH /v1/encounters/{id}` |
| `listPrescriptions` | `GET /v1/prescriptions` — paged; `status=`, `search=`, and `counts` per status |
| `createPrescription` | `POST /v1/encounters/{id}/prescriptions` |
| `dispense` | `POST /v1/pharmacy/prescriptions/{id}/dispense` |
| `createLabOrder` | `POST /v1/encounters/{id}/lab-orders` |
| `listLabOrders` | `GET /v1/lab/orders` — paged; `status=`, `unverified=`, `search=` |
| `startLabOrder` / `saveLabResults` / `verifyLabOrder` | `POST /v1/lab/orders/{id}/start`, `.../results`, `PATCH /v1/lab/orders/{id}` |
| `listInventoryItems` / `listInventoryTx` / `listInventoryBatches` | `GET /v1/inventory/items`, `/transactions`, `/batches` |
| `recordStockMovement` | `POST /v1/inventory/transactions` |
| `listInvoices` / `getInvoice` / `createInvoice` | `GET /v1/billing/invoices` (paged; `status=`, `unpaid=`, `search=`, `counts`), `GET|POST /v1/billing/invoices/{id}` |
| `addPayment` / `issueInvoice` / `voidInvoice` | `POST /v1/billing/invoices/{id}/payments`, `.../issue`, `.../void` |
| `createTenant` / `setTenantStatus` | `POST /v1/platform/tenants`, `PATCH /v1/platform/tenants/{id}/status` — a new clinic has every module at once; nothing is seeded or switched on |
| console module switches and prices | **removed, no replacement** — `/v1/platform/tenants/{id}/features[/{key}]` and `/v1/platform/catalog/features[/{key}]` are gone. The one price is `subscription_fee_monthly` on `GET|PATCH /v1/platform/settings` (super admin), echoed as `fee_monthly` on `GET /v1/platform/billing` |
| `listJobs` / `getJob` (console) | `GET /v1/platform/jobs`, `GET /v1/platform/jobs/tasks/{id}` — super admin only |
| `openFlowerSession` / `closeFlowerSession` | `POST` / `DELETE /v1/platform/jobs/flower-session` |

### Paged lists

`/v1/patients`, `/v1/appointments`, `/v1/encounters`, `/v1/prescriptions`, `/v1/lab/orders` and
`/v1/billing/invoices` all answer with `{items, total, limit, offset}` — searched, sorted and
sliced in the database, under this clinic's row-level policy, so a total can never describe rows
the caller may not read. `sort=` names one of a fixed set of keys (anything else falls back to the
default) and `order=asc|desc` turns it round.

`limit=0` means *the whole collection*, which is what the views that are not pages ask for: the
appointment day and week grids, a patient's own history, the rail's badge counts. Everything else
asks for a page — the list screens ask for ten.

Where a screen has tabs, the page carries `counts` keyed by status, taken *before* the status
filter is applied, so "Pending 12" stays true while the Dispensed tab is the one on screen.
Billing adds `unpaid`, which is a question about money rather than a status value.


### Background jobs

`GET /v1/platform/jobs` answers with the whole screen in one read: the tiles, the filter options,
and a page of jobs. It takes `status`, `task`, `worker`, `since`, `until`, `search`, `limit` and
`offset`, and the summary is counted *before* the filters — the same rule as `counts` on the
clinical lists, so "Failed 3" stays true while the Active tab is on screen.

Two things to get right:

* **`403`, not `401`.** Every clinic session is refused here on who is asking, not on whether the
  session is good, and so is a platform account whose `role` is `support`. A client that treats
  403 as "the session expired" will sign the person out of a perfectly good console. The role is
  on `platform_user.role`, so the nav can hide the section — but hiding it is presentation, and
  the API refuses the route either way.
* **Times are UTC, ISO-8601.** Not `YYYY-MM-DD HH:mm` in a clinic's timezone like everything else:
  a job belongs to no clinic, so it has no local clock, and seconds matter when half of what is on
  screen is a duration. Convert in the browser. The `since` / `until` filters read a stamp without
  a zone as UTC, so a `datetime-local` value has to go through `new Date(value).toISOString()`
  before it is sent — otherwise the window slides by the reader's offset from Greenwich.

Flower's own interface is proxied at `/v1/platform/jobs/flower/…` and is behind the same check.
A frame or a tab pointed at it cannot send an `Authorization` header, so `POST .../flower-session`
sets a short-lived HttpOnly cookie first and returns the path to open; that cookie opens the proxy
and nothing else. `FLOWER_URL_PREFIX` on the API must be the path the browser uses — behind the
`/api` rewrite that is `api/v1/platform/jobs/flower` — because Flower builds its own links from it.

## 4. Realtime — the queue socket

`WS /v1/ws/queue?access_token=…` pushes `queue.checked_in`, `queue.walk_in` and
`queue.status_changed`. The payload is deliberately thin — an entry id, a token number, a status
— because the client's job on hearing one is to re-read the queue through the ordinary endpoint:

```ts
// src/lib/realtime.ts
if (message.type.startsWith("queue.")) {
  queryClient.invalidateQueries({ queryKey: qk.queue(tenantId) });
  queryClient.invalidateQueries({ queryKey: qk.appointments(tenantId) });
}
```

That keeps one description of what the queue is, and it means the socket carries no patient data
at all. Notes for any client:

* **Browsers cannot set headers on a socket**, so the access token goes in the query string. It
  is short-lived, and the server closes the socket with **4440** when it expires — reconnect with
  a fresh one. **4401** means the token and the address name different clinics; **4403** means
  the role lacks `queue:read`.
* **WebSockets do not go through a Next rewrite.** The browser opens them directly, so the web
  app reads `NEXT_PUBLIC_API_WS_ORIGIN` in development and falls back to its own origin in
  production, where the API shares it.
* **Reconnect by re-reading.** `useQueueStream` backs off (1s → 30s), re-reads on the way back
  up, and re-reads when a backgrounded tab becomes visible again, so a screen left running in a
  waiting room recovers from a dropped connection without anyone touching it.
* It reports `connecting | live | offline`; the topbar shows that as a dot, and says so in words
  only when the connection has dropped.

## 5. Reading at scale

Two things changed shape once the data was real rather than seeded in the browser:

**The register is paged.** `usePatientPage({search, limit, offset, sort, order, …})` is one
cache entry per query, so going back to page 1 is instant and the previous page stays on screen
while the next is in flight. `<DataTable server={…}>` puts the table in manual mode — it stops
filtering, sorting and slicing what it was handed, and reports each change back instead.
Searching is debounced before it is sent.

**Nothing derives analytics in the browser any more.** `useAnalytics(days)` is one request, and
the panels that render are the ones the API sent — and since every clinic has every module,
`modules` always carries `pharmacy`, `lab`, `inventory`, `billing` and `advanced_reports` (with the
money taken out for a role without `revenue:read`). The client never decides what to compute. The old
`src/lib/analytics.ts` is gone, along with `dashboardMetrics` and the client-side patient
history: `GET /patients/{id}/history` composes that across every module server-side.

Two consequences worth knowing:

* Rows carry `patient_name` and `patient_mrn`, so lists render names without the register.
  `<PatientRef>` takes those directly; `<PatientChip>` still takes a whole patient where one has
  been fetched.
* Screens that need a *whole* patient — the record, the prescription drawer, the invoice
  composer — call `usePatient(id)` for that one row.

## 6. What changes above the seam

Small, and all of it in the same direction — the server is now the authority:

* **Ids are UUID strings.** They were already opaque strings, so nothing breaks; the demo's
  `p1`/`e3` become UUIDs.
* **`User.password` is gone.** The **Demo accounts** disclosure on the login screens should list
  the accounts as text rather than reading them from a row.
* **`initialData` seeds come off.** The configuration and tenant queries now start as pending, and the shell shows its loading state instead — the front-end README already
  anticipates this.
* **`conflictsFor` becomes async.** It is a request now, so the booking dialog awaits it.
* **There is no `hasFeature`.** Every clinic has every module, and `GET /v1/auth/me` carries no
  `features` list. What shapes the UI is `permissions`; the server enforces the same ones, so a
  route a role may not use answers `403 forbidden` regardless of what the nav shows.
* **Errors have a shape.** `{ "error": { "code", "message", "details" }, "request_id" }` —
  `message` is written to be shown to a person, so a toast can print it directly. Worth handling
  by code: `forbidden` (role), `account_on_hold` (unpaid subscription bill), `account_locked`,
  `rate_limited`, `conflict` (a state machine or a booking clash refused the change).
* **Analytics can come from the server.** `GET /v1/analytics/dashboard` returns the core metrics,
  every module panel, and clinical-type figures from `clinical_ext`. The
  client-side `lib/analytics.ts` derivations stay for the charts that need per-day series.
* **"Today" is the clinic's.** `GET /v1/tenant` carries `today` and `timezone`, and the app sets
  its formatter from those — so a practice and the screen in its waiting room never disagree
  about the date, whatever the browser's clock says.
* **The clinic switcher lost its list.** Signed in at one address you cannot enumerate the
  others; the API would not answer if asked. The console link stays.

## 7. Reference

`GET /docs` when the API is not in production mode — the full OpenAPI document, every request
and response model, with the demo data behind it.
