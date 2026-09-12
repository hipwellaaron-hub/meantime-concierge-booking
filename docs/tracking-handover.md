# Meantime Concierge tracking: audit, fixes and handover

**Scope.** Standard function enquiries for Meantime Hamilton, taken at
https://book.meantime.com.au/enquire. Nothing here covers Paddles & Pals
or Meantime The Entrance, and no event for either is introduced.

**One change since this was written (2026-09-12).** The form now lives at
a per-venue path, `https://book.meantime.com.au/enquire/hamilton`, because
a second venue is being added. `https://book.meantime.com.au/enquire`
still works and **301s to it carrying the whole query string**, so every
link and ad in this document stays correct and `utm_*`/`gclid` still reach
the form. A URL checker that reports a redirect on the bare link is seeing
that hop, not a fault. Nothing else here changes.

**Audience.** The website developer (meantime.com.au) and the
analytics/account reviewer (GTM, GA4, Google Ads, Meta).

**Versions.**

| What | Value |
|---|---|
| Deployed production (at audit) | commit `e3e4074`, Railway deployment `c32bd8d0` |
| This work | branch `feat/tracking-completion`, commits `20f0165` and `4bf0963` (review fixes), migration `f7d2c4a9b1e3` |
| Deployment of this work | pending Aaron's go; see "Rollout" |

No passwords, API secrets, real customer records or access tokens appear
in this document. Where an example payload needs a value, it is a
placeholder.

---

## 1. Audit result

| Requirement | Status | Evidence |
|---|---|---|
| Browser tags load only on the public enquiry funnel | **Implemented and verified** | `_tracking_head.html` is included by `enquiry.html` and `enquiry_submitted.html` only (grep of every template). Live: `/enquire` serves `gtag/js?id=G-XM8C86CGM6` and `fbq('init','7461755457239404')`; `/admin` serves neither. Document, invoice, wizard and staff templates contain no tag. |
| GA4 `function_enquiry_submitted` fired once per backend-confirmed enquiry | **Implemented and verified** | Thank-you page renders the snippet only for a public enquiry whose browser has not yet confirmed dispatch; the browser beacons `/enquiries/{id}/conversion/ga4` after `gtag` runs; the server flips `ga4_conversion_dispatched_at` once. Tests in `tests/test_tracking.py`. Account receipt was verified on 2026-09-01 (two host-isolated test events). |
| Meta `Lead` browser event, same guard | **Implemented and verified** | Same mechanism, `meta_conversion_dispatched_at`, `eventID` = booking reference. Live receipt verified 2026-09-01. |
| Server-side GA4 dispatch | **Was missing; now implemented, unverified in the account** | `app/services/conversions.py::dispatch_ga4_fallback`, Measurement Protocol, sent only when the browser has not confirmed within 30 minutes. Payload validated at Google's `/debug/mp/collect` (no validation messages, nothing ingested). Needs `GA4_API_SECRET` to go live. |
| Server-side Meta dispatch | **Was missing; now implemented, unverified in the account** | `dispatch_meta`, Conversions API v21.0, same `event_id` as the pixel. Needs `META_CAPI_ACCESS_TOKEN`; a `META_CAPI_TEST_EVENT_CODE` routes to Test Events for the controlled test. |
| Route by which Google Ads receives the conversion | **Implemented but unverified (account side)** | Concierge carries no Google Ads tag (`AW-…`) and no GTM container, so the only route is GA4 key-event import: GA4 property `G-XM8C86CGM6` → linked Google Ads account → conversion action `7725993486`. Whether that action is configured as a GA4 import of `function_enquiry_submitted` cannot be read from the application; see the reviewer tasks. Do not add a direct Ads tag: it would double count with the import. |
| Lead persistence, reference code, UUID | **Implemented and verified** | `Booking.id` (UUIDv4) and `Booking.reference_code` (`HAM-YYYYMMDD-XXXXX`) are created in one transaction with the contact; the thank-you URL is the UUID; the analytics event id is the reference code. |
| First-touch and last-touch storage | **Implemented and verified; now fed by parent-domain cookies** | `first_touch_attribution` / `last_touch_attribution` JSONB. Before this work the enquiry page's own capture was the only input; see section 2. |
| `mt_touch_*` cookie contract | **Specified and enforced; awaiting the website's tag** | Section 2 is final and testable: required fields, a conformance value to paste, a four-step browser self-test, and the drop rules for a missing, unreadable or future `captured_at` — enforced by `attribution._cookie_touch` and five tests, not by convention. Concierge's side needs no further change; the website tag is stage 1 of section 10. |
| First/last-touch definition | **Resolved and documented** | Ordering is by `captured_at` across every candidate; the cookie *names* are advisory. Concierge's window is the cookie lifetimes, not GA4's lookback, so the two will legitimately disagree — section 2, "How Concierge builds first and last touch". |
| GA4 fallback ambiguity | **Documented, bounded and measurable** | Meta dedupes on `event_id`; GA4's Measurement Protocol cannot. The atomic claim on `ga4_conversion_dispatched_at` makes the channels exclusive when each acts; three residual duplicate paths and one opposite under-count remain, each named with its direction, likelihood, detection and reconciliation in section 4, with a SQL query that bounds the over-count. |
| `function_enquiry_submitted` and the Ads route preserved | **Verified in the diff; account confirmation outstanding** | The branch's diff against the two browser tag partials is **empty**, so the event's name, trigger and firing are byte-for-byte what is live. Only `enquiry_type`'s value is constrained (`safe_event_type`), a no-PII measure that cannot affect a conversion counting the event. Section 5. Ads-side confirmation is task 8. |
| Submission idempotency | **Was window-only; now identity-based** | 15-second duplicate window under a Postgres advisory lock stays. New: a per-render `submission_id` (UUIDv4, unique column) resolves the same submission to the same lead regardless of time. |
| Independent GA4/Meta dispatch state and retry | **Was browser-only; now per platform and channel with retry** | `conversion_dispatches` table: one row per (booking, platform, channel), status, attempts, `next_attempt_at`, receipt. Sweep `python -m app.dispatch_conversions` retries failed sends with backoff (5 min, 15, 45, 2 h 15, then ~7 h, max 8 attempts). |
| Staging suppression | **Implemented** | Browser tags render only when `GA4_MEASUREMENT_ID` / `META_PIXEL_ID` are set; server sends only when `TRACKING_SERVER_DISPATCH_ENABLED=true` and the secret is set. Staging must set none of these. There is currently no staging environment on Railway; the gate is what protects a future one or a local copy of the production database. |
| Staff and tokenised pages excluded | **Implemented and verified** | See row one; `/d/{token}`, `/i/{token}`, wizard and admin templates never include the tag partials. |
| No PII in analytics events | **Implemented and verified** | Event parameters: `lead_id` (reference), `venue`, `source_system`, `enquiry_type`. Tests assert the email, name, phone and free text never appear in the snippet, the Meta body or the GA4 body. |
| No hashed contact matching | **Implemented** | Meta `user_data` carries only `fbp`, `fbc`, user agent and client address. No email/phone hashing exists or is planned. |
| Analytics never blocks the enquiry | **Implemented and verified** | The form is a plain server-rendered POST; every parse of cookie or attribution data is wrapped so malformed input yields "no attribution", never an error. Test: `test_tracking_does_not_alter_the_enquiry_form`. |
| Controlled live test end to end | **Blocked** | Needs an agreed test identity, the Meta test event code, and a GA4 API secret. Procedure in section 6. |

---

## 2. Website → Concierge attribution handoff

### What was proven live on 2026-09-06

Landing on `https://meantime.com.au/functions.html?gclid=…&utm_source=…`
then following one of the site's own bare links to
`https://book.meantime.com.au/enquire`:

| Cookie visible on `book.meantime.com.au` | Set by | Carries |
|---|---|---|
| `_ga` | GTM's GA4 tag on meantime.com.au | GA4 client id, e.g. `GA1.1.974737554.1785361318` |
| `_ga_XM8C86CGM6` | same | GA4 session for the property, e.g. `GS2.1.s1788681860$o19…` |
| `_gcl_aw` | GTM's Google conversion linker | `GCL.<unix seconds>.<gclid>` |
| `_gcl_au` | same | Google Ads linker id |
| `_fbp` | Meta pixel | Meta browser id |
| `_fbc` (when `fbclid` present) | Meta pixel | `fb.1.<unix millis>.<fbclid>` |

So the cookies are scoped to the registrable domain and **are** shared.
Consequences:

- **GA4 session continuity works with no change.** Both sites use the
  same measurement id, so the enquiry pages join the same session and
  GA4 attributes `function_enquiry_submitted` to the campaign that
  brought the visitor to meantime.com.au. Google Ads, via GA4 import,
  sees that attribution.
- **Concierge's own lead record did not.** The enquiry page captured
  `referrer: https://meantime.com.au/` and no click id, even though the
  gclid sat in `_gcl_aw`. That is fixed on the Concierge side: the POST
  reads `_gcl_aw`, `_gcl_gb`, `_gcl_gf`, `_fbc` and the website's
  `mt_touch_*` cookies (below), each as a touch of its own.
- **UTM parameters are not in any Google or Meta cookie.** For them to
  reach Concierge's lead record, the website has to hand them over. That
  is the one thing the website developer needs to build.

### Handoff contract (what the website must do)

Precedence on the Concierge side is fixed and the website does not need
to know it: a campaign parameter on the `/enquire` URL itself, then the
cookies below, then the referrer. Nothing the website does can override
a parameter that is on the URL.

**1. Keep the bare links.** `https://book.meantime.com.au/enquire` stays
as it is. Do not add `_gl` link decoration and do not add a GA4
cross-domain linker: the shared cookie already gives continuity, and a
`_gl` parameter would be captured as a query string. Optional and
harmless: appending `utm_*`/`gclid` parameters to the link when they are
known.

**2. Set two first-party cookies on landing when a campaign parameter is present.**

| Name | `mt_touch_first` and `mt_touch_last` |
|---|---|
| Domain | `.meantime.com.au` (parent domain, so `book.` reads it) |
| Path | `/` |
| Expiry | first: 90 days from first set, never refreshed; last: 30 days, refreshed on each new campaign touch |
| Flags | `Secure`; `SameSite=Lax`; not `HttpOnly` (the tag sets it from JavaScript) |
| Format | base64url (RFC 4648 §5, padding optional) of a JSON object |
| Size | keep under 1 KB; Concierge truncates any field to 500 characters |

JSON object keys, all optional strings:

```json
{
  "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "functions-spring",
  "utm_content": "ad-a", "utm_term": "function venue newcastle",
  "gclid": "…", "gbraid": "…", "wbraid": "…", "fbclid": "…",
  "referrer": "https://www.google.com/",
  "captured_at": "2026-09-06T08:04:20Z"
}
```

Rules for the website's tag:

- A **touch** is a page load whose URL carries any `utm_*`, `gclid`,
  `gbraid`, `wbraid` or `fbclid`, or whose referrer is off-site. Internal
  navigation is not a touch.
- On a touch: write `mt_touch_last`; write `mt_touch_first` only if it
  does not exist.
- Never write either cookie on a load with no signal. Never merge a new
  parameter into an existing cookie: a touch is written whole or not at
  all.
- Do not put anything else in the cookie. No email, no name, no page
  path with a token.

**`captured_at` is required, and "required" is enforced.** It must be the
UTC time the touch happened, as an ISO 8601 string. A cookie is **ignored
entirely** — not repaired, not guessed at, not sorted to the end — when
`captured_at` is:

| Case | Example | Why it is dropped rather than defaulted |
|---|---|---|
| absent | `{"utm_source": "google"}` | Concierge would otherwise stamp the time of the POST, which makes a cookie of unknown age the *newest* touch. `mt_touch_first` — the oldest thing the website knows — would then win **last** touch. |
| unreadable | `"yesterday"`, `""`, `"2026-13-45"` | An unreadable time is unreadable. Ordering is the cookie's only job. |
| a unix number | `1788681860` | ISO 8601 only. Numbers are rejected on purpose, so a seconds/millis mix-up fails loudly instead of landing in 1970 or 58,600 AD. |
| in the future | more than 12 hours ahead of server time | These cookies are client-controlled and cost nothing to forge; a date in 2030 would win last touch on every enquiry forever. 12 hours is deliberate slack: a phone with a wrong clock is a real visitor, not an attack. |

Enforced by `attribution._cookie_touch`, and by
`test_a_site_touch_cookie_with_no_captured_at_is_not_a_touch`,
`…with_an_unreadable_captured_at…`,
`test_a_captured_at_in_the_future_cannot_claim_last_touch`,
`test_a_linker_cookie_dated_in_the_future_is_ignored_too` and
`test_a_visitors_clock_running_an_hour_fast_is_still_a_real_visitor` in
`tests/test_tracking_completion.py`. The same rule applies to Google's
and Meta's linker cookies, so one forged `_gcl_aw` cannot take last touch
either.

**A conformance value the developer can test with.** This is the encoding
of the JSON example above, and it is what a correct tag produces:

```
eyJ1dG1fc291cmNlIjoiZ29vZ2xlIiwidXRtX21lZGl1bSI6ImNwYyIsInV0bV9jYW1wYWlnbiI6ImZ1bmN0aW9ucy1zcHJpbmciLCJnY2xpZCI6IlRFU1RHQ0xJRDEyMyIsInJlZmVycmVyIjoiaHR0cHM6Ly93d3cuZ29vZ2xlLmNvbS8iLCJjYXB0dXJlZF9hdCI6IjIwMjYtMDktMDZUMDg6MDQ6MjBaIn0
```

Set it as `mt_touch_last` on `.meantime.com.au`, submit a test enquiry,
and the booking's last-touch record reads `utm_source: google`,
`utm_medium: cpc`, `utm_campaign: functions-spring`, `gclid:
TESTGCLID123`, `referrer_category: search`, `source: cookie:mt_touch_last`.
Padding is optional (`=` may be stripped, as here). Anything over 4 KB is
ignored; keep it under 1 KB.

**The self-test, before handing it back.** With the tag live, in a
browser console on `meantime.com.au`:

1. Land on `meantime.com.au/?utm_source=test&utm_medium=handover` in a
   fresh profile. `document.cookie` shows both `mt_touch_first` and
   `mt_touch_last`, and `atob(...)` of either decodes to JSON with a
   `captured_at` within seconds of now.
2. Navigate internally (any in-site link, no parameters). Neither cookie
   changes — that is the "internal navigation is not a touch" rule.
3. Land again with `?utm_source=test2`. `mt_touch_last` changes,
   `mt_touch_first` does **not**.
4. `document.cookie` on `book.meantime.com.au` shows the same two values.
   If it does not, the Domain attribute is wrong (it must be the parent
   domain, not `www.`).

A minimal implementation is a GTM Custom HTML tag on All Pages, or a
15-line script in the site template. Concierge already reads these
cookies; nothing else changes on the Concierge side.

**3. Nothing else.** The Google click id and Meta click id reach Concierge
through Google's and Meta's own linker cookies, which GTM already sets;
the website should keep the conversion linker tag enabled.

### How Concierge builds first and last touch

Inputs, each producing a separate touch bundle:

1. The enquiry page's own capture (existing): URL parameters, referrer,
   kept in `localStorage` across the visit.
2. `_gcl_aw` → `{gclid, captured_at: cookie timestamp}`; `_gcl_gb` →
   gbraid; `_gcl_gf` → wbraid (the last two are read defensively; only
   `_gcl_aw` was observed live).
3. `_fbc` → `{fbclid, captured_at}`.
4. `mt_touch_first`, `mt_touch_last` as above.

Rules (`app/services/attribution.py::reconcile_touches`):

- A bare hop from meantime.com.au to the enquiry page with no campaign
  parameter is **not** a touch. It is navigation inside one site.
- Every other touch is a candidate, including an untagged direct return:
  a visitor who came from an ad three days ago and then types the address
  has a last touch of "unknown", which is the truth.
- **First touch = the earliest candidate by `captured_at`. Last touch =
  the latest.** A click recorded by the linker cookie on the website
  before the visitor reached Concierge is therefore the acquisition.
- **The cookie names are advisory, not authoritative.** `mt_touch_first`
  is not automatically Concierge's first touch and `mt_touch_last` is not
  automatically its last: both are candidates, ordered by their own
  `captured_at` against every other candidate. This is deliberate. The
  website only sees the website; Concierge also sees the enquiry page's
  own capture and the two ad platforms' linker cookies, so it is the only
  place that can order the whole journey. If the website's tag ever
  disagrees with Concierge about which touch is "first", Concierge is
  right by construction and no fix is needed on either side.
- **The window is the cookie lifetime, and it is not GA4's.** Concierge
  can only order touches it can still see: 90 days for `mt_touch_first`,
  30 for `mt_touch_last`, 90 for `_gcl_aw`, 90 for `_fbc`. A visitor whose
  first ad click was four months ago has a first touch of whatever
  survives, which may be a later touch. GA4 and Google Ads use their own
  lookback windows and their own model, so **Concierge's channel report
  and GA4's attribution will disagree, legitimately, and neither is
  wrong**. Concierge's report answers "what did this booking's own journey
  look like"; GA4 answers "how should credit be modelled across all
  traffic". Do not reconcile them to each other.
- Fields are never combined across touches. A gclid from one click and
  UTMs from another visit remain two bundles.
- The same click seen twice (URL and cookie) is one touch.
- Missing, malformed or expired input: ignored. If no candidate has a
  signal, the page's own bundles stand (`referrer_category: unknown` or
  `referral`).

Stored shape (both columns):

```json
{"utm_source": null, "utm_medium": null, "utm_campaign": null, "utm_term": null, "utm_content": null,
 "gclid": "…", "gbraid": null, "wbraid": null, "fbclid": null,
 "referrer": null, "referrer_category": "unknown", "captured_at": "2026-09-06T08:04:20+00:00",
 "source": "cookie:_gcl_aw"}
```

`source` names the cookie a touch came from; a touch the enquiry page
captured itself has no `source`. A cookie-derived touch whose
`captured_at` cannot be read, or which claims to be from the future, is
dropped at the boundary rather than guessed at — see the contract table
above.

### GA4 client and session continuity, separately

Concierge does not manage GA4 identity. The shared `_ga` and
`_ga_XM8C86CGM6` cookies give gtag on the enquiry pages the same client
and session. Concierge only *reads* them on the POST, into
`tracking_context`, so a server-side fallback can be tied to the same
client and session. It never writes them.

---

## 3. Conversion authority

| Rule | Mechanism | Test |
|---|---|---|
| A successful enquiry exists only after persistence | The thank-you redirect is issued after `create_enquiry_booking` commits. No event fires on the form page. | `test_thanks_renders_snippet_but_writes_nothing_at_render` |
| A CTA click is intent; opening the form is nothing | No event on `/enquire` beyond page view. | Live: `/enquire` renders no conversion snippet. |
| Validation failure → no conversion | 422, no booking, no thank-you page. | `test_validation_failure_creates_no_booking_no_conversion` |
| Thank-you reload → no second conversion | Snippet rendered only while the platform's `*_conversion_dispatched_at` is NULL; the browser's `localStorage` key is the same-browser guard. | `test_browser_dispatch_beacon_records_then_refresh_does_not_refire` |
| Retry after lost response → original lead | `submission_id` (per-render UUID) is stored in the same INSERT as the booking and looked up first, under the email advisory lock, but honoured only while it is the same submission (same email, event name and date); the form re-mints the id on a back-forward restore, so Back, edit, resubmit is a new enquiry. Falls back to the 15-second content window when absent. | `test_the_same_submission_id_resolves_to_the_same_lead_after_the_window`, `test_the_same_submission_id_with_different_content_is_a_new_lead`, `test_the_submission_id_is_written_in_the_same_insert_as_the_booking` |
| Concurrent repeats → one lead | Postgres transaction advisory lock on the email, plus the identity lookup. | `test_concurrent_repeats_with_one_submission_id_create_one_lead`, `test_concurrent_identical_enquiries_create_one_booking` |

Identity chain: `submission_id` (browser, per render) → `bookings.id`
(UUIDv4, the thank-you URL) → `reference_code` (business reference) →
analytics `lead_id` / Meta `event_id` / GA4 `lead_id` = `reference_code`.

Dedup per destination, stated honestly:

- **Meta**: browser and server copies share `event_name = Lead` and
  `event_id = reference_code`; Meta deduplicates on that pair within its
  window. Both copies are sent by design.
- **GA4**: has no event-id dedup. Only one copy is ever sent: the browser
  copy, or the server fallback if the browser did not confirm within the
  grace period. The fallback claims the send atomically (the same
  NULL-flip the beacon uses) before it posts, so a thank-you page loading
  during the sweep stops offering the browser copy; a failed post
  releases the claim. Residual limitation: if the browser fired but its
  confirmation beacon was lost, the fallback will send a second event.

---

## 4. Independent dispatch and recovery

`conversion_dispatches` rows, unique on (booking, platform, channel):

| platform | channel | Written when | Status meanings |
|---|---|---|---|
| ga4 / meta | browser | the thank-you page's beacon | `sent` (receipt never visible) |
| meta | server | background task after persistence; sweep retries | `accepted` (Meta returned `events_received`), `failed` (+`next_attempt_at`), `skipped` (+reason). No row at all while dispatch is off or unconfigured, so an enquiry from before the variables were set is still sent once they exist (within the 7-day window). |
| ga4 | server | sweep, after the grace period | `sent` (Measurement Protocol answers 204 without a receipt), `failed`, `skipped` |

If Meta fails and GA4 (browser) succeeded, GA4 is untouched and Meta
retries on its own schedule; and vice versa
(`test_one_platform_failing_does_not_touch_the_other`). An accepted or
sent row is never re-sent.

### The GA4 fallback ambiguity, stated exactly

Meta and GA4 are not symmetric, and the asymmetry is the whole of this
section. **Meta's Conversions API deduplicates** a browser and a server
copy that share `event_id` *and* `event_name`, so sending both is the
designed behaviour and a retry after a lost response costs nothing.
**GA4's Measurement Protocol does not deduplicate at all** — there is no
event id to match on and no receipt to check. So a GA4 server copy is a
*fallback*, never a companion: it may only be sent when the browser
demonstrably did not send its own.

Concierge's guard is a single atomic claim on
`bookings.ga4_conversion_dispatched_at`. The browser beacon and the sweep
both flip it from NULL under the same `WHERE … IS NULL` update, and the
loser sends nothing (`_claim_ga4`; the sweep claims *before* it POSTs).
That makes the two channels mutually exclusive **at the moment each
acts**. It cannot make them exclusive across the gap between when a page
was rendered and when its JavaScript runs, and it cannot see inside GA4.
Three residual paths remain. They are not equally likely and they do not
fail in the same direction:

| # | Path | Direction | Frequency | How to see it | How to reconcile |
|---|---|---|---|---|---|
| 1 | **Beacon lost.** `gtag` ran and GA4 counted it, but the browser's confirmation POST to Concierge never arrived — tab closed in the same instant, connection dropped. The flag stays NULL, so the sweep sends after the grace period. | **Over**-counts by one | The likeliest of the three, and still rare: the beacon is same-origin and fires immediately after `gtag`. | A `ga4 / server / sent` row on a booking with **no** `ga4 / browser` row. | Compare that count against GA4's own `function_enquiry_submitted` total for the period. Concierge's booking count is the truth; GA4 high by the number of such rows. |
| 2 | **Lost 204.** The server POSTed, GA4 ingested it, but the response never came back. `_release_ga4` puts the flag back to NULL and the sweep retries. | **Over**-counts by one | Rare. | A `ga4 / server` row with `attempts > 1` and a `transport:` error in `last_error`, later `sent`. | Same as 1. `attempts > 1` on a `sent` row is the marker. |
| 3 | **Stale rendered page.** The thank-you page was rendered while the flag was NULL (so it carries the snippet), the tab then sat idle past the grace period, the sweep claimed and sent, and the tab's `gtag` ran afterwards. | **Over**-counts by one | Rarest. Requires a tab open and un-executed for ≥30 minutes. | A `ga4 / browser` row whose `created_at` is later than the `ga4 / server` row's `sent_at` on the same booking. | Same as 1. Raising `GA4_SERVER_FALLBACK_AFTER_MINUTES` shrinks this path and widens no other. |

The opposite error also exists and is **not** a duplicate: a browser that
fired `gtag` and beaconed back, where GA4 itself dropped the event
(consent mode denying storage, a blocked request after the beacon).
Concierge records the browser send and correctly never sends a server
copy, so GA4 is short by one. **This under-count is invisible from the
application side** and cannot be distinguished from path 1 without GA4's
own numbers.

**Net effect and the honest bound.** All three duplicate paths need a
failure between two systems in a window of minutes, so the expected error
is small and one-directional (GA4 slightly high), against an under-count
of unknown size in the other direction (consent-blocked browsers, GA4
slightly low). **The two do not cancel and must not be reported as if
they do.** Neither is measurable without the GA4 account, which is why
GA4 reconciliation is an account-side task (section 8), not an
application one.

**One lever worth knowing about.** The server copy carries `session_id`
from the `_ga_XM8C86CGM6` cookie when it is readable. If the Google Ads
conversion action for `function_enquiry_submitted` is set to count
**one per session** — task 8 asks the reviewer to confirm this — then a
duplicate from paths 1–3 collapses in Ads even though GA4 still shows two
events. That protects the number that actually drives bidding. It is
stated here as a property to confirm, not one this work has verified:
Concierge cannot see the Ads account.

**If a duplicate is ever unacceptable**, the fallback is a single switch
rather than a rewrite: leave `GA4_API_SECRET` unset. Meta's server copy
is unaffected, because Meta dedupes.

Reconciliation: the admin booking page lists every row; SQL for a period:

```sql
select b.reference_code, d.platform, d.channel, d.status, d.attempts, d.sent_at, d.last_error
from conversion_dispatches d join bookings b on b.id = d.booking_id
where b.created_at >= now() - interval '30 days' order by b.created_at, d.platform, d.channel;
```

The three GA4 duplicate paths above, counted for a period — run this
before comparing anything to GA4's own totals:

```sql
-- 1: server sent with no browser row. 2: a retry that had already landed.
-- 3: a browser row created after the server had already sent.
select
  count(*) filter (where srv.status = 'sent' and br.id is null)                     as path_1_beacon_lost,
  count(*) filter (where srv.status = 'sent' and srv.attempts > 1)                  as path_2_lost_204,
  count(*) filter (where srv.status = 'sent' and br.created_at > srv.sent_at)       as path_3_stale_page
from bookings b
join conversion_dispatches srv
  on srv.booking_id = b.id and srv.platform = 'ga4' and srv.channel = 'server'
left join conversion_dispatches br
  on br.booking_id = b.id and br.platform = 'ga4' and br.channel = 'browser'
where b.created_at >= now() - interval '30 days';
```

Their sum is the most GA4 can be over for the period. It says nothing
about the under-count from consent-blocked browsers, which is not
visible here.

---

## 5. Event contract

| | GA4 (browser) | GA4 (server fallback) | Meta (browser) | Meta (server) |
|---|---|---|---|---|
| Name | `function_enquiry_submitted` | same | `Lead` | `Lead` |
| Trigger | thank-you page load, once, after persistence | sweep, ≥30 min after persistence, only if no browser confirmation | thank-you page load, once | background task right after persistence; sweep retries |
| Sender | gtag on `book.meantime.com.au` | Concierge → `www.google-analytics.com/mp/collect` | pixel on `book.meantime.com.au` | Concierge → `graph.facebook.com/v21.0/{pixel}/events` |
| Identity | `lead_id` = reference | `lead_id`, `client_id` from `_ga`, `session_id` from `_ga_XM8C86CGM6` | `eventID` = reference | `event_id` = reference |
| Parameters | `lead_id`, `venue=hamilton`, `source_system=meantime_concierge`, `enquiry_type` | same + `dispatch_channel=server`, `engagement_time_msec=1` | `content_category` = enquiry type | `custom_data`: `lead_id`, `venue`, `source_system`, `content_category`; `user_data`: `fbp`, `fbc`, `client_user_agent`, `client_ip_address` |
| Destination | GA4 property `G-XM8C86CGM6` → Google Ads import | same | Meta dataset `7461755457239404` | same |

Redacted example payloads:

GA4 server fallback (validated at Google's debug endpoint, 2026-09-06, no messages):

```json
{"client_id": "974737554.1785361318", "timestamp_micros": 1788680370627504, "non_personalized_ads": false,
 "events": [{"name": "function_enquiry_submitted", "params": {"lead_id": "HAM-20271114-TEST0", "venue": "hamilton",
   "source_system": "meantime_concierge", "enquiry_type": "Wedding", "dispatch_channel": "server",
   "engagement_time_msec": 1, "session_id": "1788681860"}}]}
```

Meta server copy (from the test suite's captured request; token redacted):

```json
{"data": [{"event_name": "Lead", "event_time": 1788680370, "event_id": "HAM-20271114-XXXXX",
  "action_source": "website", "event_source_url": "https://book.meantime.com.au/enquire",
  "user_data": {"client_user_agent": "Mozilla/5.0 …", "fbp": "fb.2.…", "fbc": "fb.1.…", "client_ip_address": "203.0.113.7"},
  "custom_data": {"lead_id": "HAM-20271114-XXXXX", "venue": "hamilton", "source_system": "meantime_concierge",
    "content_category": "Wedding"}}],
 "access_token": "<redacted>", "test_event_code": "<only during the controlled test>"}
```

### `function_enquiry_submitted` is preserved, not replaced

This is the event Google Ads already imports, so it is the one thing in
this work that was not free to change. It has not been changed.

| | Before this branch | After |
|---|---|---|
| Event name | `function_enquiry_submitted` | unchanged |
| Where it fires | `gtag` on the thank-you page, once, after persistence | unchanged |
| What triggers it | thank-you page load with `emit_ga4` true | unchanged |
| GA4 property | `G-XM8C86CGM6` | unchanged |
| Parameters | `lead_id`, `venue`, `source_system`, `enquiry_type` | unchanged **names**; `enquiry_type`'s **value** is now constrained (below) |
| Meta's `Lead` | pixel on the thank-you page | unchanged |

`git diff main…feat/tracking-completion -- app/templates/_tracking_conversion.html
app/templates/_tracking_head.html` is **empty**. The browser tags are
byte-for-byte what is live today, which is the mechanical proof that the
Ads import cannot have been disturbed by this branch.

The one behavioural change to the existing event is the *value* of
`enquiry_type`: it is now passed through `conversions.safe_event_type`,
so a value from the form's own list is sent as-is and anything else — an
API client's free text — is sent as `"other"` rather than verbatim. This
is a no-PII measure: the parameter could otherwise carry arbitrary
submitted text into analytics. It cannot affect the Ads conversion, which
counts the event, not this parameter. Google Ads reporting segmented *by*
`enquiry_type` would show an `other` bucket where it previously showed
free text; no such segment is known to exist, and task 8 asks the
reviewer to confirm.

Everything this work adds is **additive and namespaced away from it**: a
GA4 server copy of the same event name only as a fallback that never runs
beside a confirmed browser send (section 4), and Meta `Lead` server
copies that Meta dedupes. No new GA4 event name, no new Ads conversion
action, no Paddles & Pals event, and nothing touching Ads budgets,
campaigns, goals or account settings.

Browser snippet (rendered on the thank-you page, from `_tracking_conversion.html`):

```js
gtag('event', 'function_enquiry_submitted', {lead_id: "HAM-…", venue: 'hamilton', source_system: 'meantime_concierge', enquiry_type: "Wedding"});
fbq('track', 'Lead', {content_category: "Wedding"}, {eventID: "HAM-…"});
```

---

## 6. Tests and evidence

**Code tests (isolated, real Postgres, roll back):** `tests/test_tracking.py`
(existing, 19), `tests/test_attribution.py` (existing), `tests/test_tracking_completion.py`
(new, 51). Whole suite: 1,249 passing at commit `4bf0963`. Every new test
fails without the application changes (stash-checked). `alembic check`
reports only a pre-existing drift unrelated to this branch
(`ix_bookings_parent_booking_id`).

| Proof required | Test |
|---|---|
| Campaign → website → Concierge → lead keeps attribution | `test_linker_cookie_gclid_becomes_the_first_touch_when_the_page_saw_only_a_referral`, `test_submission_reads_the_parent_domain_cookies` |
| First and last touch as documented | `test_touches_are_ordered_by_time_and_never_merged`, `test_an_untagged_direct_return_is_still_the_last_touch`, `test_the_internal_hop_is_not_a_touch_but_an_external_referral_is`, existing `test_gclid_captured_at_first_landing_survives_to_a_later_submission` |
| Invalid submission → no conversion | `test_validation_failure_creates_no_booking_no_conversion` |
| Lost response + retry → one lead | `test_the_same_submission_id_resolves_to_the_same_lead_after_the_window` |
| Concurrent duplicates → one lead | two threading tests (real sessions and commits) |
| Thank-you reload → no additional conversion | `test_browser_dispatch_beacon_records_then_refresh_does_not_refire`, `test_dispatch_beacon_is_idempotent` |
| One provider fails → other not duplicated | `test_one_platform_failing_does_not_touch_the_other`, `test_transport_failure_then_retry_succeeds_and_does_not_resend_after` |
| Staging / excluded pages send nothing | `test_nothing_is_sent_and_nothing_is_marked_when_server_dispatch_is_off`, `test_tags_render_only_when_configured`, `test_staff_booking_thanks_does_not_emit_and_rejects_beacon` |

**Outbound dispatch evidence (no ingestion):** GA4 Measurement Protocol
payload accepted by `https://www.google-analytics.com/debug/mp/collect`
with `validationMessages: []` (2026-09-06). Meta Conversions API: not yet
exercised, no token available. The GA4 API secret travels as a query
parameter by Google's design; the httpx request log, which prints full
URLs, is silenced in the application so it cannot reach Railway's logs
(`test_the_secret_bearing_request_log_is_silenced`).

**Live, read-only:** cookie sharing proven from the browser (section 2);
`/enquire` carries both tags; `/admin` carries none; a random thank-you
UUID answers 404.

**Receipt in the account:** GA4 and Meta receipt of the *browser* events
was verified on 2026-09-01. Receipt of the *server* copies is not yet
verified; it requires the controlled test below.

### Controlled live test (to be run with Aaron's authorisation)

Preconditions: an agreed test identity (name, a mailbox Aaron controls,
event name prefixed `ZZ TEST`), `META_CAPI_TEST_EVENT_CODE` set to the
code shown in Events Manager → Test Events, `GA4_API_SECRET` set,
`TRACKING_SERVER_DISPATCH_ENABLED=true`. The enquiry notification email
will go to the venue inbox as for any enquiry; that is the one
side-effect and it must be agreed.

1. Land on `https://meantime.com.au/functions.html?utm_source=test&utm_medium=handover&gclid=TESTGCLID` in a fresh browser profile; browse two pages; follow a bare link to `/enquire`; submit with the test identity.
2. Expected in Concierge: one booking; first touch `gclid=TESTGCLID` from `_gcl_aw`; `tracking_context` with `ga_client_id`; a `conversion_dispatches` row `meta/server/accepted` with a `fbtrace_id` within a minute; GA4 `browser/sent` after the thank-you page loads.
3. Expected in Meta Events Manager → Test Events: one `Lead` with the reference as event id, from both Browser and Server, marked deduplicated.
4. Expected in GA4 Realtime / DebugView: one `function_enquiry_submitted` with `lead_id` = reference.
5. Reload the thank-you page: no new row, no new event. Submit the same form again from the same tab: same thank-you URL.
6. Block the GA4 tag (browser extension) in a second submission, wait 35 minutes, run the sweep: expect `ga4/server/sent` and `ga4_conversion_dispatched_at` set; in GA4 one event.
7. Delete the test bookings afterwards (staff), and annotate the GA4 property for the date.

---

## 7. Configuration and account identifiers (non-secret)

| Item | Value | Where |
|---|---|---|
| GA4 measurement id | `G-XM8C86CGM6` | Railway `GA4_MEASUREMENT_ID` on the web service; GTM on meantime.com.au |
| Meta pixel / dataset | `7461755457239404` | Railway `META_PIXEL_ID`; GTM on meantime.com.au |
| Main website GTM container | `GTM-5JVMW52H` | meantime.com.au only; not on Concierge |
| Google Ads conversion action | `7725993486` | Google Ads; route is GA4 import (to be confirmed) |
| Event | `function_enquiry_submitted` | GA4 key event; Google Ads import source |
| Server dispatch opt-in | `TRACKING_SERVER_DISPATCH_ENABLED=true` | Railway, production only |
| Meta CAPI token | `META_CAPI_ACCESS_TOKEN` (secret) | Railway; system-user token with `ads_management` for the dataset |
| Meta test code | `META_CAPI_TEST_EVENT_CODE` | Railway, set only during the controlled test |
| GA4 API secret | `GA4_API_SECRET` (secret) | Railway; GA4 Admin → Data streams → Measurement Protocol API secrets |
| Grace period | `GA4_SERVER_FALLBACK_AFTER_MINUTES` (default 30) | Railway, optional |
| Sweep | `python -m app.dispatch_conversions` | new Railway cron service; recipe below |
| Environment key | `RAILWAY_ENVIRONMENT_NAME` | set by Railway; anything but `production` disables tags and sends |
| Public base URL | `PUBLIC_BASE_URL=https://book.meantime.com.au` | not yet set; branch `fix/short-client-links` unmerged |

### Railway recipe for the sweep (infrastructure, not code)

Copy the digest service. Its live configuration was read from Railway on
2026-09-06 and is reproduced here so the sweep can be built to match
rather than from memory:

```
service        meantime-concierge-digest   (project a97a452b…, environment production)
source repo    hipwellaaron-hub/meantime-concierge-booking
builder        RAILPACK          buildEnvironment V3      runtime V2
startCommand   python -m app.send_digest
cronSchedule   0 21 * * *
preDeploy      (none)
restartPolicy  NEVER
region         sfo × 1 replica
variables      AI_API_TOKEN, DATABASE_URL, DIGEST_API_KEY, DIGEST_FROM_EMAIL,
               DIGEST_GMAIL_ADDRESS, DIGEST_GMAIL_APP_PASSWORD,
               DIGEST_RECIPIENT_EMAIL, SECRET_KEY
```

The sweep is the same shape with a different command, schedule and
variable set:

| Setting | Value | Why this value |
|---|---|---|
| Service name | `meantime-concierge-conversions` | |
| Source | same repo, branch `main` | |
| Builder / runtime | RAILPACK, build env V3, runtime V2 | match the digest; these are the project defaults |
| Start command | `python -m app.dispatch_conversions` | |
| Cron schedule | `*/15 * * * *` | The GA4 grace period is 30 minutes, so a 15-minute pass sends a fallback 30–45 minutes after the enquiry. Anything slower widens duplicate path 3. |
| Restart policy | **NEVER** | A cron service that restarts on exit runs continuously. This is the setting that matters most; get it wrong and the sweep re-runs in a loop. |
| Pre-deploy command | **none** | Migrations belong to the web service alone. A second service running `alembic upgrade head` would race it on deploy. |
| Region / replicas | `sfo`, 1 | **Exactly one replica.** Two would sweep the same bookings concurrently. The per-booking claim and the unique `(booking, platform, channel)` index make that safe rather than duplicating, but it is wasted work and needless contention. |
| Health check | none | It exits; there is nothing to check. |

Variables to set on the sweep service — and only these:

| Variable | Value | Note |
|---|---|---|
| `DATABASE_URL` | same as the web service | |
| `SECRET_KEY` | same as the web service | required by `Settings`; unused by this command |
| `TRACKING_SERVER_DISPATCH_ENABLED` | `true` | without it the command logs "disabled here" and exits 0 |
| `GA4_MEASUREMENT_ID` | `G-XM8C86CGM6` | |
| `META_PIXEL_ID` | `7461755457239404` | |
| `META_CAPI_ACCESS_TOKEN` | secret | omit and Meta sends are simply skipped |
| `GA4_API_SECRET` | secret | omit and the GA4 fallback never runs — the deliberate opt-out from section 4 |
| `GA4_SERVER_FALLBACK_AFTER_MINUTES` | omit (defaults to 30) | raise it to shrink duplicate path 3 |
| `META_CAPI_TEST_EVENT_CODE` | during the controlled test only | **unset it afterwards**; test events do not count |

`RAILWAY_ENVIRONMENT_NAME` is set by Railway itself — do not set it by
hand. It is the second gate: anything but `production` disables every
send even with all the variables present.

**Verifying the service without sending anything.** Create it with
`TRACKING_SERVER_DISPATCH_ENABLED` unset first. The next run logs
`Server-side conversion dispatch is disabled here; nothing to do.` and
exits 0. That proves the schedule, the build and the database connection
in one pass, with no possibility of a real conversion. Then set the
variable.

The **web** service needs the same tracking variables too: the first Meta
copy is sent by the web process right after persistence. A push does not
deploy either service; request a build for both.

---

## 8. Account-side tasks (for the reviewer; no changes made by this work)

**GTM (GTM-5JVMW52H on meantime.com.au)**
1. Confirm the Google Ads Conversion Linker tag fires on all pages (it does today: `_gcl_aw` was set live).
2. Add the `mt_touch_first` / `mt_touch_last` Custom HTML tag per section 2, or hand the contract to the website developer.
3. Confirm no GA4 event named `function_enquiry_submitted` fires from GTM itself (it must come only from Concierge, or it double counts).

**GA4 (G-XM8C86CGM6)**
4. Mark `function_enquiry_submitted` as a key event (if not already).
5. Create a Measurement Protocol API secret for the web stream and give it to Aaron for `GA4_API_SECRET`.
6. Confirm cross-domain settings list `meantime.com.au` and `book.meantime.com.au` (harmless either way given the shared cookie, but it stops referral exclusions from splitting sessions).
7. Add `book.meantime.com.au` to "unwanted referrals"? No: the referral is from the same registrable domain and GA4 already treats it as internal.

**Google Ads**
8. Confirm conversion action `7725993486` is a GA4 import of `function_enquiry_submitted` from property `G-XM8C86CGM6`, counted "one per session", included in Conversions, and that the GA4 link and auto-tagging are on.
9. Do not add a website conversion tag for Concierge: it would double count.

**Meta (dataset 7461755457239404)**
10. Generate a Conversions API system-user access token for the dataset and give it to Aaron for `META_CAPI_ACCESS_TOKEN`.
11. Provide the Test Events code for the controlled test.
12. After the test, confirm in Events Manager that browser and server `Lead` events show as deduplicated.

---

## 9. Outstanding limitations

- **Server copies are dormant until the two secrets are set** and the opt-in is enabled. Until then behaviour is exactly the deployed browser-only path.
- **GA4 fallback can double count.** Three paths, all rare, all in the same direction, all detectable — and one opposite under-count that is not detectable from here. Section 4 states each one with its frequency and a query that bounds the total. Unsetting `GA4_API_SECRET` removes the possibility entirely, at the cost of the fallback.
- **GA4 fallback without a client id** (a visitor with no `_ga` cookie, e.g. a consent-blocked browser) is skipped, recorded as such, and the browser copy stays on offer.
- **The Google Ads route is unverified from the application side.** Concierge cannot see the Ads account; task 8 closes this.
- **UTMs from the website require the cookie contract in section 2.** Until the website sets `mt_touch_*`, only click ids cross over; UTM-only campaigns show as first touch "referral" in Concierge's own report, while GA4 attribution is unaffected. This is a smaller answer, never a wrong one, and it needs no Concierge change when the tag ships.
- **A cookie with no usable `captured_at` is discarded, not repaired.** That is the right trade — the alternative silently made the oldest cookie the newest touch — but it means a website tag that omits the field produces *no* attribution rather than partial attribution, and does so silently. The self-test in section 2 is what catches it; there is no server-side alarm for a tag that never ships a valid cookie.
- **`_gcl_gb` / `_gcl_gf` names** for gbraid/wbraid are read defensively; only `_gcl_aw` was observed live.
- **Client address and user agent** are stored in `tracking_context` for Meta matching, never shown in the UI or export, and cleared by the sweep 14 days after the enquiry (the pseudonymous cookie ids stay for reconciliation). The address is the one the trusted proxy reported and is kept only if it parses as an address. Sending them to Meta's Conversions API is a disclosure the privacy policy should name.
- **Enquiry type** reaches a payload only when it is one of the form's own values; anything else (an API client's free text) is sent as `other`.
- **No Railway staging environment exists.** Two keys protect a future one: the ids and secrets are unset there, and `RAILWAY_ENVIRONMENT_NAME` must be `production` for a tag to render or a server send to go, even if every variable was copied.
- **The retry sweep needs a cron service** to exist; without it, a failed Meta send is retried only by the next background task for a different enquiry (never), and the GA4 fallback never runs.

---

## 10. Controlled rollout

Five stages. Each one is separately reversible, and **no stage can send a
production conversion until stage 4** — the two secrets and the opt-in
are all absent until then, and each is a separate gate. Do not compress
stages: the point of the order is that if something is wrong, the stage
it is wrong in is the stage that shows it.

**Stage 0 — merge and deploy the code. No behaviour change.**

1. Re-parent the migration if `feat/beo-proposals` merged first: `f7d2c4a9b1e3` and `a3f6e1c7d094` both descend from `b4c1e8f27a93`, so whichever lands second needs its `down_revision` moved to the other. Two heads will fail the deploy's pre-deploy step, loudly and before serving.
2. Merge to `main`, request a Railway build of the **web** service (a push does not deploy).
3. Pre-deploy runs `alembic upgrade head` → `f7d2c4a9b1e3`: adds `bookings.submission_id` (+ unique index), `bookings.tracking_context`, and the `conversion_dispatches` table. Additive only; nothing is dropped or rewritten, so it is safe on a live database.
4. **Gate:** submit one ordinary enquiry. It behaves exactly as today. Cookie-derived attribution and `submission_id` now work; `conversion_dispatches` gains only `browser` rows. **Zero server sends are possible — no secrets are set.**
   Rollback: redeploy the previous build. The new columns are unused by the old code and can stay.

**Stage 1 — the website's cookies (independent of everything else).**

5. Hand section 2 to the website developer. Nothing on the Concierge side changes; Concierge has read these cookies since stage 0.
6. **Gate:** the developer's own self-test in section 2 (four checks in a browser console), then one enquiry from a `?utm_source=…` landing shows that `utm_source` as first touch on the booking. Until this passes, only click ids cross over — a smaller result, never a wrong one.
   Rollback: remove the tag. Concierge degrades to click-ids-only with no error.

**Stage 2 — the sweep service, deliberately inert.**

7. Create `meantime-concierge-conversions` per section 7 **with `TRACKING_SERVER_DISPATCH_ENABLED` unset**.
8. **Gate:** its first scheduled run logs `Server-side conversion dispatch is disabled here; nothing to do.` and exits 0. Schedule, build, database connection and restart policy are all proven with no possibility of a send. Confirm the run **ended** — a service still running after its log line has the wrong restart policy.
   Rollback: delete the service.

**Stage 3 — the controlled live test, on test events only.**

9. Agree the test identity and arrangement with Aaron first (section 6). The venue notification email is a real side-effect and must be agreed before, not explained after.
10. Set `META_CAPI_TEST_EVENT_CODE`, `META_CAPI_ACCESS_TOKEN`, `GA4_API_SECRET`, `TRACKING_SERVER_DISPATCH_ENABLED=true` on both the web and sweep services.
11. Run section 6's seven-step procedure.
12. **Gate:** Meta Events Manager shows the browser and server `Lead` as **deduplicated** — that single word is what licences sending both. GA4 DebugView shows exactly one `function_enquiry_submitted`. `conversion_dispatches` matches. If dedup does not show, stop here: unset `META_CAPI_ACCESS_TOKEN` and diagnose before stage 4.
    Rollback: unset the two secrets. Everything reverts to browser-only.
13. Delete the test bookings; annotate the GA4 property for the date.

**Stage 4 — production counting.**

14. Unset `META_CAPI_TEST_EVENT_CODE` on both services. This is the moment real conversions begin; nothing before it counted.
15. **Gate, at 24 hours and again at 7 days:** run section 4's diagnostic query. Expect `path_1_beacon_lost` to be a small fraction of enquiries and paths 2 and 3 at or near zero. A large path 1 means the beacon is not getting back — investigate before trusting the GA4 total. Compare Concierge's booking count for the period against GA4's event count and Ads conversions; they will not match exactly, and section 4 says which directions the differences run in.
    Rollback: unset `GA4_API_SECRET` to stop GA4 fallbacks alone (Meta is unaffected, because Meta dedupes), or `TRACKING_SERVER_DISPATCH_ENABLED=false` to stop all server sends. Neither needs a deploy or a code change.

**What is deliberately not in this rollout:** no change to Google Ads
budgets, campaigns, conversion goals or account settings; no new
conversion action; no hashed contact-data matching; no Paddles & Pals
events; no staging analytics (there is no Railway staging environment,
and `RAILWAY_ENVIRONMENT_NAME` gates any future one). Advertising scripts
remain off staff pages and off tokenised host/guest wizard pages.
