# Google Ads offline conversions — the shape of the work

**Not built. This is a scoping document only**, written because gclid is
now being captured on every enquiry (see `app/services/attribution.py`),
which is the one thing this depends on and didn't exist before.

## Why this matters

Right now, Google Ads only knows about the *click*. It optimises a
campaign toward whatever it can measure — clicks, or at best a form
submission if a conversion tag is on the enquiry page. It has no idea
which of those enquiries turned into an actual confirmed booking, or what
that booking was worth. A campaign genuinely driving profitable bookings
and a campaign driving nothing but tyre-kickers look identical to Google
Ads today, because the commercial outcome lives here, in Concierge, not
there.

Offline conversion import closes that loop: it tells Google Ads, after
the fact, "this specific click (identified by gclid) resulted in a real
conversion, worth this much, on this date." Google's bidding algorithms
(especially Target ROAS / Maximize Conversion Value) use that to shift
spend toward whatever's actually producing it — the kind of optimisation
that's structurally impossible without this data.

## What it depends on

1. **gclid capture on the booking that confirms** — done. `Booking.
   first_touch_attribution` / `last_touch_attribution` now carry it when
   present.
2. **A Google Ads "conversion action" configured for import**, created in
   the Google Ads UI (Tools & Settings → Conversions → New conversion
   action → Import → Other data sources or CRMs → Track conversions from
   clicks). This defines the category (e.g. "Purchase"), the attribution
   model, and the count setting (one per click). Nothing uploads
   anywhere without this existing first, and it's a five-minute UI task
   on Aaron's side, not something this codebase can do for him.
3. **API access**: a Google Ads *developer token* (applied for inside the
   Google Ads account — approval can take a few days for anything beyond
   a test account), an OAuth client (Google Cloud project + client
   ID/secret), and a refresh token authorising access to the specific
   Ads account. All of this becomes new Railway environment variables,
   the same pattern as `STRIPE_SECRET_KEY` / `DIGEST_GMAIL_APP_PASSWORD`
   already established in this app.
4. **A real client library or direct REST calls** against the Google Ads
   API's conversion upload service (`ConversionUploadService`, uploading
   `ClickConversion` records keyed by gclid). Google publishes an
   official Python client; using it rather than hand-rolling REST calls
   is the sane default.

## The timing constraint that actually matters

Google Ads only accepts an offline conversion if it falls within the
conversion action's **click-through lookback window** — configurable up
to 90 days. A booking that enquires, gets its deposit paid, agreement
signed, wizard completed, and finally confirms can genuinely take that
long in slow cases. Most won't; some will. Anything confirming outside
the window simply can't be backdated into Google Ads — worth knowing
going in, not discovering as a surprise gap in the numbers later.

## The shape of the implementation, when it's time

Same architecture as the two periodic/reconciliation patterns already in
this codebase — the digest (`app/send_digest.py`, a scheduled Railway
service) and the enquiry-notification retry-and-surface pattern
(`app.services.enquiry_classification.notify_new_enquiry`):

1. A new `Booking.google_ads_conversion_uploaded_at` column — the same
   set-once-timestamp pattern already used for `viewed_at` / `opened_at`
   / `enquiry_notification_sent_at` elsewhere on this model. NULL means
   "not yet uploaded"; that's the self-clearing worklist condition.
2. A query: bookings that reached `confirmed` status, have a gclid in
   either touch, and have no upload timestamp yet.
3. A scheduled job (new `app/upload_ad_conversions.py`, its own Railway
   cron service, same as the digest) that calls the Google Ads API for
   each, uploads the click conversion (gclid, conversion action resource
   name, conversion date-time — the *confirmation* time, not the click
   time — and ideally the real booking value, likely the confirmed
   deposit or total invoice amount, so Google optimises toward value, not
   just count), and on success sets the upload timestamp.
4. Failure handling identical in shape to what already exists for the
   enquiry notification: log it, don't silently drop it, surface anything
   that failed on a Triage-style worklist so a human can see and retry it
   — never a fire-and-forget upload with no visibility into what didn't
   land.

## What this explicitly does not do

Doesn't touch pricing, routing, or which policy applies to a booking —
same rule as the rest of the attribution capture this depends on. This
is a one-way report *to* Google Ads about what already happened; nothing
comes back from it into how Concierge treats a booking.
