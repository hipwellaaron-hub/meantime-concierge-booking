# Meantime Concierge tracking: audit, fixes and handover

**Scope.** Standard function enquiries for Meantime Hamilton, taken at
https://book.meantime.com.au/enquire. Nothing here covers Paddles & Pals
or Meantime The Entrance, and no event for either is introduced.

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

`captured_at` is required and must be the UTC time the touch happened
(ISO 8601). Concierge orders touches by it.

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
captured itself has no `source`. A touch whose `captured_at` cannot be
read is left out of the ordering rather than guessed.

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

Ambiguous outcomes: a provider that accepted before the response was lost
is recorded `failed` with a transport error and retried; for Meta the
retry deduplicates on `event_id`; for GA4 the fallback is only attempted
when no send has been recorded, so a lost 204 leads to one retry and a
possible duplicate. Both are stated in section 9.

Reconciliation: the admin booking page lists every row; SQL for a period:

```sql
select b.reference_code, d.platform, d.channel, d.status, d.attempts, d.sent_at, d.last_error
from conversion_dispatches d join bookings b on b.id = d.booking_id
where b.created_at >= now() - interval '30 days' order by b.created_at, d.platform, d.channel;
```

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

The digest already runs this way (`meantime-concierge-digest`: own
service, own variables, `restartPolicyType: NEVER`, no pre-deploy). The
sweep needs the same:

| Setting | Value |
|---|---|
| Service | new, from `hipwellaaron-hub/meantime-concierge-booking`, branch `main` |
| Start command | `python -m app.dispatch_conversions` |
| Cron schedule | `*/15 * * * *` |
| Restart policy | NEVER |
| Pre-deploy command | none |
| Variables | `DATABASE_URL`, `SECRET_KEY` (required by Settings), `GA4_MEASUREMENT_ID`, `META_PIXEL_ID`, `TRACKING_SERVER_DISPATCH_ENABLED=true`, `META_CAPI_ACCESS_TOKEN`, `GA4_API_SECRET`, `DASHBOARD_BASE_URL`, and `META_CAPI_TEST_EVENT_CODE` only during the test |

The **web** service needs the same tracking variables too: the first
Meta copy is sent by the web process right after persistence. A push
does not deploy either service; request a build for both.

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
- **GA4 fallback can double count** when the browser fired but its beacon was lost. Expected to be rare; visible as a `ga4/server/sent` row on a booking whose browser row is missing.
- **GA4 fallback without a client id** (a visitor with no `_ga` cookie, e.g. a consent-blocked browser) is skipped, recorded as such, and the browser copy stays on offer.
- **The Google Ads route is unverified from the application side.** Concierge cannot see the Ads account; task 8 closes this.
- **UTMs from the website require the cookie contract in section 2.** Until the website sets `mt_touch_*`, only click ids cross over; UTM-only campaigns show as first touch "referral" in Concierge's own report, while GA4 attribution is unaffected.
- **`_gcl_gb` / `_gcl_gf` names** for gbraid/wbraid are read defensively; only `_gcl_aw` was observed live.
- **Client address and user agent** are stored in `tracking_context` for Meta matching, never shown in the UI or export, and cleared by the sweep 14 days after the enquiry (the pseudonymous cookie ids stay for reconciliation). The address is the one the trusted proxy reported and is kept only if it parses as an address. Sending them to Meta's Conversions API is a disclosure the privacy policy should name.
- **Enquiry type** reaches a payload only when it is one of the form's own values; anything else (an API client's free text) is sent as `other`.
- **No Railway staging environment exists.** Two keys protect a future one: the ids and secrets are unset there, and `RAILWAY_ENVIRONMENT_NAME` must be `production` for a tag to render or a server send to go, even if every variable was copied.
- **The retry sweep needs a cron service** to exist; without it, a failed Meta send is retried only by the next background task for a different enquiry (never), and the GA4 fallback never runs.

---

## 10. Rollout

1. Merge `feat/tracking-completion` to main; deploy (pre-deploy runs `alembic upgrade head`, migration `f7d2c4a9b1e3`, additive only).
2. Behaviour after deploy with no new variables: identical to today plus cookie-derived attribution and submission identity. No server sends.
3. Set `META_CAPI_TEST_EVENT_CODE`, `META_CAPI_ACCESS_TOKEN`, `GA4_API_SECRET`, `TRACKING_SERVER_DISPATCH_ENABLED=true`; create the cron service; run the controlled test (section 6).
4. Unset `META_CAPI_TEST_EVENT_CODE`. Production counting begins.
