# The Entrance: launch checklist

Nice Try Events Pty Ltd, trading as **Meantime The Entrance**. A different
company from Meantime Pty Ltd — its own ABN, bank account and Stripe
account. One deployment, one database, entity carried as data.

Nothing in this repository executes any of it. It is the order to do it in,
what each step protects against, and what the system will tell you if you
skip one.

**The rehearsal is `tests/test_the_entrance_can_open.py`.** It creates the
venue through the same admin page you will use and drives a booking all the
way to a paid deposit, asserting that none of Hamilton's fourteen
client-facing details appear on the other company's documents. If you change
any of the steps below, change it there first and watch it fail.

---

## Settled: both companies trade on the same terms

Asked and answered, 2026-09-15: **identical, nothing to build.**

Venue IDENTITY is on the venue row. Venue POLICY is not, and that is now a
decision rather than an accident. Every generated agreement carries the
same figures whatever venue it belongs to:

| Figure | Value | Where it prints |
| --- | --- | --- |
| Deposit | $500 | The deposit clause, the frozen `deposit_required`, and the deposit invoice |
| Event Order lead time | 14 days | Two clauses, and the wizard's own deadline |
| Short-notice cancellation | $20 per head | Two cancellation clauses |
| Guest shortfall | $50 per adult | The minimum-spend clause |
| AV deadline | 2 days | The Event Order |

Minimum spend and minimum adults are **not** in this list — they come from
the `spaces` row and have always been per venue, so The Entrance's own room
carries its own numbers without any of the above changing.

`tests/test_both_venues_trade_on_the_same_terms.py` pins it. Changing any
of these figures now changes **both** companies' contracts, and adding a
per-venue policy column fails that file on purpose — not to forbid it, but
so the decision is made out loud rather than arrived at.

## The order

Each step is inert on its own. Nothing here can take a real payment until
step 9.

### 1. Create the venue

**Venues** in the admin nav → *Add a venue*. Name `The Entrance`, slug
`entrance`.

The slug can never be changed: it is in the admin URL of every page about
the venue, in the Stripe endpoint path its account posts to, and in
`AI_VENUE_SLUG`. The page also creates the **Unassigned (pending triage)**
space, which is not optional — a venue without it serves its enquiry form
as a normal page and then 500s on the submit, losing the lead with no
booking and no notification.

*From this moment `/healthz` reads `degraded` and the 20:30 digest carries a
VENUE SET-UP INCOMPLETE section every night until step 4 is done. That is
the intended behaviour, not a fault.*

### 2. Set the reference prefix

`ENT`, on the same page. **Do this before the venue takes anything**: it
locks the moment the first booking or invoice is issued, because every
reference built from it is frozen and never rewritten.

Without it the venue cannot take a booking at all (a `ValueError` in the
reference generator) or issue an invoice (a `RAISE` in the invoice trigger).

### 3. Add the room(s)

Still a database job — the venue page deliberately does not invent a room,
because capacity, minimum spend and minimum adults are business decisions.

Each space needs `name`, `capacity`, `min_food_spend`,
`standard_min_adults`, `wheelchair_accessible` and `is_bookable`.

### 4. Fill in the identity

On the venue page: trading name, legal name, ABN, address, phone, contact
name, contact email, bank account name, BSB, account number, licence
number, licensed manager, trading days.

**There is no fallback for any of these on purpose** — nothing substitutes
another company's details. A blank one prints blank on an invoice, an
agreement and an Event Order, and on payment the blank is *frozen* into the
receipt.

When this is complete, `/healthz` returns to `ok` (except for step 9) and
the digest's set-up section disappears.

### 5. Point the marketing site at `/enquire/entrance`

The bare `/enquire` and the legacy `POST /enquiries` both resolve to
**Hamilton**, deliberately, so that ads already in the wild keep working.
That means an Entrance ad, button or QR code pointing at either of them
files every lead at the wrong company, silently. Check every link.

### 6. Floor accounts

**Staff** → create the floor accounts on **The Entrance's** staff page, so
they carry its venue. A floor account with no venue is refused at sign-in
with a message saying so.

Known gap, worth knowing before you use it: moving an existing person
between venues by re-submitting their email rewrites their venue but does
**not** revoke the device tokens they already hold, so their phone keeps
showing the old venue's run sheets until the token is revoked by hand.

### 7. The AI credential

Add `,entrance` to `AI_VENUE_SLUG` on the **MCP service**. It is
comma-separated and fails loudly (503) on a slug that does not exist, so a
typo is visible immediately.

AI drafting stays off at both venues — that is a separate switch and a
separate decision, revisited in a few months.

### 8. Stripe, in this order

The order matters. Doing it any other way leaves a window where a real card
is charged and the payment is never recorded.

1. In **Nice Try Events' own Stripe account**, switch to Live mode.
2. Create the webhook endpoint:
   `https://book.meantime.com.au/webhooks/stripe/entrance`, sending
   `checkout.session.completed`. Copy the signing secret.
3. On Railway's **web service**, set `STRIPE_WEBHOOK_SECRET_ENTRANCE` to
   that secret.
4. On the venue page, set **Stripe webhook secret env** to
   `STRIPE_WEBHOOK_SECRET_ENTRANCE` — the variable NAME, never the secret.
5. On Railway, set `STRIPE_SECRET_KEY_ENTRANCE` to Nice Try Events' own
   secret key.
6. On the venue page, set **Stripe secret key env** to
   `STRIPE_SECRET_KEY_ENTRANCE`.

`/healthz` reports `stripe_webhook_ready` and `stripe_live_mode` folded
across every venue that can mint a link, so it will tell you if 3–6 are
half done. It cannot tell you whether the endpoint exists in Stripe: that
is push-only and there is no reconciliation job for it.

### 9. Pin the account — its own day, after the deploy has settled

Set **Stripe account id** on the venue page to Nice Try Events'
`acct_...`.

Until this is set, `assert_key_belongs_to` checks nothing, and a mis-keyed
credential mints the payment link inside the **wrong company's** Stripe
account — which then signs its own completion event, passes verification,
and records as a successful payment. This is the one guard between a
mis-keyed credential and money landing in the wrong company.

`/healthz` reads `stripe_account_pinned: false` until it is done, for
Hamilton as well.

---

## What the system will tell you

| Surface | What it covers |
| --- | --- |
| `/healthz` | venue set-up complete; Stripe key present, live, webhook secret set, account pinned; schema drift; the AI gates |
| The 20:30 digest | the set-up gaps by name, with what each one costs; overdue invoices; reconciliation findings; a wizard that produced no Event Order |
| Triage | the same findings, per venue |

What none of them covers: whether the webhook endpoint exists in Stripe,
and whether the marketing site's links point at the right venue.

---

## Known, recorded, not fixed

These are real and were found deliberately. None blocks the launch; all are
worth knowing before you meet them.

- **The floor logo is one file for both companies.** Every run sheet and
  floor PDF renders Hamilton's logo, whatever the venue.
- **Both venues share one GA4 property and one Meta pixel**, and the
  parent-domain ad cookies cross between them — so a Hamilton ad click can
  be attributed to an Entrance lead. A decision, not a defect, but it is
  one company's ad spend being measured against another's leads.
- **Every notification is sent from Hamilton's Gmail account.** The
  recipient is per venue; the sender is not.
- **`classify_lead_source` treats `meantime.com.au` as "own website".** If
  The Entrance's pages live on another domain, its own leads record as
  referrals.
- **Contacts are shared between venues** (a recorded decision) — and an
  existing contact's name and phone are *not* updated by a later enquiry,
  so the name on file stays whatever the first venue captured.
- **If you give The Entrance its own `digest_recipient_email`**, the digest
  splits into two emails and *neither carries a venue heading* — the
  heading only renders when a group holds more than one venue.
