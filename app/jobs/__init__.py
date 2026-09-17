"""Background jobs for platform billing.

Six jobs, each a module, each a `run()` that can be called as often as you
like. They share three properties, and none of the three is a matter of the
code remembering to check:

*   **Idempotent.** Every write they make is protected by a unique index — one
    cycle per `(subscription, period_start)`, one non-void invoice per cycle,
    one payment per gateway payment id, one webhook row per event id. Running a
    job twice produces the same database as running it once.
*   **Retryable.** A failure on one clinic is caught, logged and left; the
    other clinics still get their invoices. Nothing here rolls a batch back
    because one row in it was bad.
*   **Tenant-bound.** Each clinic is processed with `app.tenant_id` set to that
    clinic, so row-level security applies to a cron job exactly as it applies
    to a request. There is no privileged read anywhere in this package —
    `app/services/messaging/worker.py` set the pattern and this follows it.

`runner.py` is the loop that calls them on a schedule; `scripts/billing_worker.py`
is the process. A deployment that prefers its own scheduler can call each
`run()` directly and ignore both.
"""

from app.jobs import (  # noqa: F401
    generate_monthly_invoices,
    process_billing,
    process_razorpay_webhook,
    process_subscription_renewals,
    send_invoice_email,
    sync_razorpay_subscription,
)

#: The jobs by name, for the runner and for anything that schedules by string.
JOBS = {
    "GenerateMonthlyInvoicesJob": generate_monthly_invoices.run,
    "ProcessBillingJob": process_billing.run,
    "ProcessSubscriptionRenewalsJob": process_subscription_renewals.run,
    "SendInvoiceEmailJob": send_invoice_email.run,
    "ProcessRazorpayWebhookJob": process_razorpay_webhook.run,
    "SyncRazorpaySubscriptionJob": sync_razorpay_subscription.run,
}

__all__ = ["JOBS"]
