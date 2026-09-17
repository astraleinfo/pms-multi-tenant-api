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
| `listFeatures` | `GET /v1/tenant/features` |
| `purchaseFeature` / `deactivateFeature` | `POST /v1/tenant/features/{key}/purchase` / `.../deactivate` |
| `getConfig` / `updateConfig` | `GET` / `PATCH /v1/tenant/config` |
| `listFieldDefinitions` | `GET /v1/tenant/fields` |
| `getSchema` | `GET /v1/tenant/schema/{entity}` — the clinical type is the server's, so the argument goes away |
| `addFieldDefinition` / `removeFieldDefinition` | `POST /v1/tenant/fields` / `DELETE /v1/tenant/fields/{id}` |
| `listPatients` | `GET /v1/patients?search=&limit=&offset=&sort=&order=` — returns `{items, total, limit, offset}` |
| `getPatient` / `createPatient` / `updatePatient` | `GET|POST /v1/patients`, `PATCH /v1/patients/{id}` |
| `getPatientHistory` | `GET /v1/patients/{id}/history` |
| `listAppointments` / `createAppointment` | `GET|POST /v1/appointments` — paged; also `date=`, `doctor_id=`, `search=` |
| `conflictsFor` | `GET /v1/appointments/availability` — now async |
| `setAppointmentStatus` | `PATCH /v1/appointments/{id}/status` |
| `checkIn` | `POST /v1/appointments/{id}/checkin` |
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
| `createTenant` / `setTenantStatus` | `POST /v1/platform/tenants`, `PATCH /v1/platform/tenants/{id}/status` |

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
the panels that render are the ones the API sent — a module a clinic never bought has no key in
the response, so the client does not need a feature check to decide what to compute. The old
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
* **`initialData` seeds come off.** The entitlements, configuration and tenant queries now start
  as pending, and the shell shows its loading state instead — the front-end README already
  anticipates this.
* **`conflictsFor` becomes async.** It is a request now, so the booking dialog awaits it.
* **`hasFeature` still shapes the UI**, but the server is the enforcement: a gated route answers
  `403 feature_not_enabled` regardless of what the nav shows.
* **Errors have a shape.** `{ "error": { "code", "message", "details" }, "request_id" }` —
  `message` is written to be shown to a person, so a toast can print it directly. Worth handling
  by code: `feature_not_enabled` (offer the upgrade), `forbidden` (role), `account_locked`,
  `rate_limited`, `conflict` (a state machine or a booking clash refused the change).
* **Analytics can come from the server.** `GET /v1/analytics/dashboard` returns the core metrics,
  module panels for active features only, and clinical-type figures from `clinical_ext`. The
  client-side `lib/analytics.ts` derivations stay for the charts that need per-day series.
* **"Today" is the clinic's.** `GET /v1/tenant` carries `today` and `timezone`, and the app sets
  its formatter from those — so a practice and the screen in its waiting room never disagree
  about the date, whatever the browser's clock says.
* **The clinic switcher lost its list.** Signed in at one address you cannot enumerate the
  others; the API would not answer if asked. The console link stays.

## 7. Settings → Billing

The billing screen is one GET. `GET /v1/settings/billing` returns the plan, every module with its
price and state, the next billing date, any scheduled cancellations, and what is outstanding — so
the screen does not need to assemble itself from four requests and cannot show a half-consistent
picture while they land.

```ts
const billing = await api<BillingSummary>("/v1/settings/billing");

billing.subscription            // null until the clinic takes a plan
billing.subscription?.plan      // { code, name, amount, currency, interval }
billing.subscription?.current_period_days   // 28 | 29 | 30 | 31
billing.next_billing_on         // null while paused or cancelling
billing.recurring_subtotal      // what the next invoice comes to, before tax
billing.outstanding_total       // and what is owed right now
billing.addons                  // every module, priced and stated
```

### Rendering an add-on row

Four fields decide what the row says, and the combination that trips people up is the third:

| State | Fields | What to show |
| --- | --- | --- |
| Covered by the plan | `included: true`, `monthly_amount: null` | "Included" — not "Free", and no price |
| Off | `active: false` | the price, and a button to switch it on |
| On | `active: true`, `cancels_on: null` | the price, and a button to cancel |
| **Cancelling** | `active: true`, `cancels_on: "2026-03-01"` | "Active until 1 March" and a button to **keep** it |

That last row is the one to get right: the module is still on, still usable and still paid for.
Showing it as "cancelled" would be wrong, and offering "activate" rather than "keep" would imply a
charge that is not going to happen.

### Switching a module on and off

```ts
const result = await api<AddonAction>(`/v1/settings/billing/addons/${key}`, { method: "POST" });
```

The response says what actually happened, so the toast can be specific rather than vague:

* `charged: true` — `amount`, `prorated_days` and `cycle_days` are set, and `invoice` is the
  one-off invoice raised for it. "₹1,096.23 for 17 of 31 days" is a better message than "Done".
* `charged: false, reinstated: true` — a scheduled cancellation was dropped. **Nothing was
  charged.** Say so; a practice manager who has just clicked a button on a billing screen wants
  to know whether it cost anything.
* `charged: false, reinstated: false` — the module is covered by the plan.

`DELETE` on the same path schedules the cancellation and returns `cancels_on`. It never charges
and never refunds.

### Pay Now

Never construct an amount in the browser. `POST .../invoices/{id}/checkout` returns the gateway's
**public** key id and an order id, priced from the invoice server-side:

```ts
const checkout = await api<Checkout>(`/v1/settings/billing/invoices/${id}/checkout`, {
  method: "POST",
});

const rzp = new Razorpay({
  key: checkout.key_id,          // public by design; the secret never leaves the server
  order_id: checkout.order_id,
  amount: checkout.amount * 100, // display only — the server already priced the order
  currency: checkout.currency,
  name: checkout.customer_name,
  handler: async (response) => {
    // A claim, not a fact. The server verifies the signature, re-fetches the
    // payment from the gateway and compares the amount before anything is paid.
    await api(`/v1/settings/billing/invoices/${id}/verify`, {
      method: "POST",
      body: JSON.stringify({
        order_id: response.razorpay_order_id,
        payment_id: response.razorpay_payment_id,
        signature: response.razorpay_signature,
      }),
    });
  },
});
```

Two things follow from that, and both matter for the UI:

* **the handler firing is not proof of payment.** Re-read the invoice after `verify` returns and
  render *its* status. A `409` from `verify` means the gateway's own record did not match what
  this server asked for, and the invoice is still owing.
* **the webhook may get there first.** If the tab is closed mid-payment, the invoice still turns
  paid — both paths end at the same idempotent write. So the billing screen should be refetched
  on focus rather than assumed stale-free.

`GET .../invoices/{id}/download` returns a PDF built from the stored line items, which is why an
invoice downloaded a year from now says what it said the day it was raised.

### Errors worth handling by code

`409 conflict` covers the states a person can reach by clicking: *already active*, *not active*,
*part of the base plan and cannot be cancelled on its own*, *already settled*, *online payment is
not configured*. The `message` is written to be shown, so a toast can print it directly.

`403 forbidden` on any of these means the signed-in user is not a tenant administrator —
`SUBSCRIPTION_READ` and `SUBSCRIPTION_MANAGE` belong to that role alone. Hide the Billing item in
Settings for everyone else; the server refuses it regardless of what the nav shows.

## 8. Reference

`GET /docs` when the API is not in production mode — the full OpenAPI document, every request
and response model, with the demo data behind it.
