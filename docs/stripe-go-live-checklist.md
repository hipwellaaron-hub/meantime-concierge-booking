# Stripe go-live checklist

Concierge is currently running Stripe in **test mode**, deliberately. This
document is the procedure for switching to live mode. It is a checklist to
follow by hand when the decision to go live has been made — nothing in this
repository executes any of it automatically, and nothing here should be run
until you've decided the venue is ready to take real card payments.

The current mode is always visible in the admin UI (a banner across every
page, plus a "Payments" card on the Dashboard) — see `app/services/
stripe_integration.py::get_mode()`. It's derived from the live `STRIPE_SECRET_KEY`
value itself, not a separate setting, so it can never silently disagree with
which key is actually loaded.

## The primary risk: keys and webhook secret drifting apart

**This is the failure mode that matters most, because it's invisible.**

`STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` are two separate credentials,
and Stripe issues a **different webhook signing secret for every webhook
endpoint** — critically, **test mode and live mode each need their own
webhook endpoint configured in the Stripe Dashboard**, with their own secret.
The live secret does not exist until you create the live endpoint.

If `STRIPE_SECRET_KEY` is updated to a live key but `STRIPE_WEBHOOK_SECRET`
is left pointing at the test endpoint's secret (or no live endpoint was ever
created), here is exactly what happens:

1. A client pays via a live Payment Link. Stripe processes the real charge
   successfully — the client's card is charged, they see a real receipt.
2. Stripe tries to deliver a `checkout.session.completed` webhook to
   `/webhooks/stripe`.
3. Either there's no live endpoint registered at all (nothing is ever sent),
   or `stripe.Webhook.construct_event()` fails signature verification
   because the secret doesn't match, and the endpoint returns `400` (see
   `app/api/webhooks.py`).
4. Stripe retries a failed delivery on a backoff schedule for up to 3 days,
   then gives up.
5. `invoicing.record_payment()` is never called. The invoice sits at
   `sent`, forever, unless a human notices.

**The client was charged. The invoice says unpaid. Nothing on either side
of the integration raised an error.** This is the one outcome the order of
operations below exists to make structurally impossible, not just unlikely.

### The safe order

Do these in this order — each step is inert on its own, so there's no point
in the sequence where a real live payment could go unreconciled:

1. **In the Stripe Dashboard, switch to Live mode** (top-left toggle).
   Nothing below touches test mode.
2. **Create the live webhook endpoint**: Developers → Webhooks → Add
   endpoint. URL: `https://<production-domain>/webhooks/stripe`. Event to
   send: `checkout.session.completed` (the only event `app/api/webhooks.py`
   currently handles — see "Also worth knowing" below on what that implies
   for other event types). Copy the **signing secret** it gives you
   (`whsec_...`) — this is a live-mode secret, distinct from the test
   endpoint's.
3. **Set `STRIPE_WEBHOOK_SECRET` on Railway to the new live secret.**
   `STRIPE_SECRET_KEY` is still the *test* key at this point, so the app
   still can't create a live Payment Link — this step is a no-op in
   practice, safely, until step 4.
4. **Set `STRIPE_SECRET_KEY` on Railway to the live secret key.** From this
   moment, invoice pages start generating live Payment Links, and the
   webhook secret needed to verify the resulting events is already in
   place — no gap between the two.
5. **Redeploy** (or restart) the service. Both values are read once, at
   process start, via `os.environ.get(...)` in `app/services/
   stripe_integration.py` and `app/api/webhooks.py` — they are **not**
   re-read from Railway on the fly, and (confirmed while building the mode
   indicator) they are also **not** sourced from a local `.env` file the
   way `DATABASE_URL` is. A stale process will keep using whatever it
   already had in memory.
6. **Confirm the mode indicator now reads "Stripe live"** in the admin
   dashboard header/banner. If it still says test or not-configured, stop —
   the redeploy didn't pick up the new key, or the wrong variable was set.
7. Only now, run the end-to-end verification transaction (below).

## Second risk: Payment Links issued under test keys

Concierge does **not** store Payment Link URLs anywhere — `create_payment_link()`
generates a fresh one on every single load of `/i/{token}`, deliberately
never cached (see the docstring in `app/services/stripe_integration.py`;
the amount can shrink after a partial payment, so a cached link would go
stale). There is no table, no column, nothing in Concierge's own database
listing "links issued so far."

Two consequences:

- **Concierge itself cannot tell you which links exist.** The only place
  that list exists is the Stripe Dashboard's own Payment Links section,
  filtered to test mode.
- **This is mostly self-healing, but not entirely.** Because the link is
  regenerated on every page view, the next time *any* invoice page is
  opened after go-live, it automatically gets a fresh *live* link — nobody
  needs to manually "regenerate" anything in the normal case.

The gap is a client who already has an old test-mode link saved outside of
reopening the invoice page — copied into an email reply, a notes app, a
browser bookmark. That specific URL points at a Stripe object that is
permanently test-mode; it does not start accepting real cards after you
flip the account to live, it just keeps behaving as a test transaction
(declining any real card presented to it).

Before going live: pull the list of **sent but unpaid** invoices
(`InvoiceStatus.sent`, `is_fully_paid` false — the same set
`app.services.digest.get_overdue_invoices` already looks at for the ones
that are also overdue) and cross-reference against `Invoice.viewed_at`
(built alongside the notification work earlier) to see which of those a
client has actually opened recently. Those are the ones worth a short
follow-up email after cutover, so the client's next click lands on a live
link rather than a stale test one they might have saved.

## Third requirement: one real transaction before any client sees a live link

Do this against a **disposable test booking**, not a real client's
invoice — Concierge has no refund/reversal function (`invoicing.py` has no
`record_refund` or equivalent; `cancel_invoice()` explicitly refuses to
cancel an invoice that's already `paid`). A refund issued in Stripe does
**not** automatically reflect back into Concierge — see "Also worth
knowing" below. Using a scratch booking means it's fine if its `paid`
status in Concierge never gets cleaned up afterward.

1. Create a small deposit invoice on the scratch booking, send it.
2. Open the invoice's public link, pay the small amount with a **real
   card** (not a Stripe test card — the whole point is confirming the live
   path).
3. Confirm in the **Stripe Dashboard** (live mode) that the payment landed.
4. Confirm the **webhook actually delivered** — Developers → Webhooks →
   the live endpoint → recent deliveries should show a `200` for this
   event, within a few seconds.
5. Confirm the invoice in Concierge flipped to **paid** on its own, with no
   manual intervention.
6. **Refund** the payment from the Stripe Dashboard.
7. Manually mark the scratch invoice/booking however you'd normally close
   out a mistaken test entry in Concierge — the refund itself won't do
   this for you (see below).

Only once steps 3–5 have all been confirmed should a live Payment Link be
put in front of an actual client.

## Environment variables, named exactly

| Variable | Where it's read | What changes |
|---|---|---|
| `STRIPE_SECRET_KEY` | `app/services/stripe_integration.py` | Sandbox `sk_test_...` → Live `sk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | `app/api/webhooks.py` | Test endpoint's `whsec_...` → **new** live endpoint's `whsec_...` (created in step 2 above — this is not the same value with a different prefix, it's a wholly new secret from a wholly new endpoint) |

Both are plain Railway service variables (`railway variables --set`, or the
dashboard). Neither has a fallback or default — if either is unset, the
relevant code path fails loudly (`StripeNotConfigured` / a `503` from the
webhook route) rather than silently pretending to work.

## Also worth knowing

- **Stripe's own retry policy on a failing webhook**: up to roughly 3 days
  of backoff attempts, then it stops. After that, the only way to recover
  a specific missed event is manually, from the Stripe Dashboard's event
  log (Developers → Events → find the event → Resend). Nothing in
  Concierge prompts anyone to go and check for this.
- **A second, already-existing silent-miss path, unrelated to go-live**:
  `app/api/webhooks.py::_handle_checkout_completed` catches `ValueError`
  from `record_payment()` (e.g. the invoice was cancelled between link
  creation and payment) and returns `200` anyway, so Stripe never retries
  it — deliberately, to avoid Stripe hammering an endpoint retrying
  something that will never succeed, but the practical effect is the same
  as a missed webhook: a real Stripe payment with no corresponding
  Concierge record, discoverable only by comparing the two systems by
  hand. This exists today, independent of test vs. live mode.
- **Only `checkout.session.completed` is handled.** If a payment fails
  after the checkout session starts, gets disputed, or is refunded from
  the Stripe side, none of that reaches Concierge — there's no handler for
  `charge.refunded`, `charge.dispute.created`, or anything else. A refund
  has to be reflected in Concierge by hand, every time, indefinitely
  — not just for the go-live test transaction above.

## Report: does Concierge reconcile against Stripe if a webhook is missed?

**No. This is push-only.** The webhook handler is the *only* mechanism
that ever records a card payment. There is no scheduled job, script, or
admin action anywhere in the codebase that queries the Stripe API to check
"did we miss anything" — nothing analogous to `app/send_digest.py` exists
for Stripe. If a webhook delivery is lost and its retry window lapses
before anyone notices, that payment is never recorded automatically, full
stop. The invoice stays `sent`, and the only paths to noticing are the
Dashboard's "unpaid invoices" count / the digest's overdue-invoice section
eventually flagging it as overdue, or a human manually cross-checking the
Stripe Dashboard's own payments list against Concierge's invoices.

Push-only is a reasonable place to be for now, given the venue's transaction
volume — but it should be a known, chosen limitation, not an assumed one.
A future improvement worth scoping (not built here, not requested for this
pass): a periodic job that lists recent Stripe `checkout.session`s with
the `concierge_invoice_id` metadata key and cross-checks each against
Concierge's own payment records, the same self-clearing-worklist pattern
already used for enquiries, overdue invoices, and wizard-eligible bookings
elsewhere in this app.
