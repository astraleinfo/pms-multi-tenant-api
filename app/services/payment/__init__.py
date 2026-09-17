"""Platform billing — what a clinic pays the platform for the product.

Gateway-independent by construction. Everything in this package that decides
money — `subscription_service`, `billing_service`, `invoice/`, `proration/`,
`addon/`, `tax/`, `metrics/` — knows only `gateway.Gateway`, the protocol in
`gateway.py`. Everything that talks to a particular gateway lives under
`razorpay/` and decides nothing. Adding Cashfree or Stripe is a new package
beside `razorpay/` and one line in `gateway._ADAPTERS`.

Not to be confused with `app/services/billing.py`, which is a clinic billing
its patients. Different money, different people, different tables.
"""
