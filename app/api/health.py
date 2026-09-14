"""Public, detailed health check -- powers the small status dot in the
top-right of the admin header, and is meant to also be polled by an
external uptime monitor (UptimeRobot, Better Uptime, Railway's own
healthcheck alerting, etc.). That second consumer is the important one:
an in-app widget can only ever report "the app looked fine the last time
someone with a tab open polled it" -- if the process crashes or the whole
service goes down, there is nobody left inside it to raise the alarm.
Detecting a genuine outage needs something watching from *outside* the
process, hitting this same endpoint, and paging a human when it stops
answering. See docs/ for that half of the setup.

Deliberately a separate route from the existing bare `/health` in
app/main.py, which Railway's own deploy tooling may depend on staying a
trivial, always-200, zero-dependency check -- this one does a real query
and is allowed to report something other than "ok".

Never returns anything beyond booleans/counts -- no config values, no
secrets, nothing that would turn a public monitoring endpoint into an
information leak.
"""

import datetime as dt
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Venue
from app.services import ai_access, drafting, schema_audit, venue_readiness
from app.services import enquiry_classification, stripe_integration
from app.services.notifications import is_gmail_smtp_configured

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False

    if not database_ok:
        # The one check that matters most: if this fails, nothing else
        # below can be trusted either (it all needs the same connection).
        return {
            "status": "down",
            "checks": {"database": False},
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    try:
        # EVERY venue, not a hardcoded one. Two things were wrong here until
        # 2026-09-12, and they compounded:
        #
        #   * `filter_by(slug="hamilton").first()` -- `.first()` returns None
        #     where the eight `_venue()` helpers all use `.one()`, which
        #     raises. So a database with no Hamilton row reported
        #     notification_failures = 0 and status "ok". The one endpoint
        #     that exists to DETECT trouble was the one that failed open, and
        #     an external monitor would have seen a green light over an empty
        #     database.
        #   * Only Hamilton was ever asked. A second venue's failing enquiry
        #     notifications would not have shown up at all.
        #
        # No venue is NAMED in the response: this endpoint is public, its own
        # docstring forbids leaking config, and which companies operate here
        # is not a fact a monitoring URL should hand out. One folded boolean.
        # Ordered, so the log lines below come out in the same order as
        # seed.report_every_venue's -- the deploy log and the runtime one
        # are read side by side when a venue is being set up.
        venues = db.scalars(select(Venue).order_by(Venue.slug)).all()
        if not venues:
            # A database with no venue cannot serve anybody. Degraded, loudly.
            return {
                "status": "degraded",
                "checks": {"database": True, "venues_present": False},
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        notification_failures = sum(
            len(enquiry_classification.get_enquiry_notification_failures(db, venue))
            for venue in venues
        )
        drafting_failures = drafting.recent_failure_count(db)
        schema_drifting = schema_audit.drifting(db)
        # Read through the service, not off the row, so the ENV override
        # counts: ai_access.access_enabled returns False when the env var
        # says so whatever the database holds, and a page that reported the
        # row alone would say the gate was open while it was shut.
        ai_row = ai_access.get_settings_row(db)
        ai_gate_open = ai_access.access_enabled(db)
        ai_gates = {
            "ai_access_enabled": ai_gate_open,
            "ai_writes_enabled": ai_access.writes_enabled(db),
            # THE EFFECTIVE gate, not the row's own column -- the same
            # expression drafting.draft_for_booking:332 evaluates before it
            # will write a draft. Reporting the column alone would read
            # "drafting on" while the master switch above it was shut. Which
            # of the two is shut stays derivable, because ai_access_enabled
            # is reported beside it.
            "ai_drafting_enabled": bool(
                ai_gate_open and ai_row.drafting_enabled
            ),
        }
        # Is every Stripe key pinned to the account it must belong to?
        # stripe_integration.assert_key_belongs_to is the guard between a
        # mis-keyed credential and a payment recorded as successful for
        # the wrong company -- and it returns early when the venue has no
        # stripe_account_id. That is a deliberate rollback window, a
        # numbered go-live step, and deliberately absent from the seed's
        # unfilled-columns report. It was also absent from everywhere else,
        # so a guard that is a no-op today had nothing reminding anyone to
        # arm it. A venue with no key has nothing to pin and is not counted.
        stripe_account_pinned = all(
            bool((getattr(venue, "stripe_account_id", None) or "").strip())
            for venue in venues
            if stripe_integration.is_configured_for(venue)
        )
        # Can every venue's completion events be VERIFIED? A venue can mint
        # a payment link with a perfectly good key and have no signing
        # secret set for the endpoint its own Stripe account posts to. The
        # webhook handler refuses, correctly -- but the refusal lands in
        # Stripe's delivery history and a log line, and Stripe retries for
        # about three days and then drops the event: a client charged, an
        # invoice still saying unpaid, nothing raised on either side.
        #
        # Only asked of venues that CAN mint a link, and that asymmetry with
        # `stripe_configured` below is deliberate rather than an oversight:
        # "can this venue take a card" is a business state a venue is allowed
        # to be in (reported, not degrading), while "this venue already takes
        # cards and cannot record the payments" is a fault. A venue taking no
        # cards loses nothing. A venue taking cards it cannot record loses
        # money silently. That is what tells the two apart.
        #
        # A venue with no Stripe key at all has nothing to verify. That
        # makes it vacuously true wherever Stripe is unconfigured -- which
        # is every test environment, so the probes patch is_configured_for,
        # the same trap the preview test fell into on 2026-09-14.
        #
        # NOT in the digest, and that is not an oversight. This reads
        # environment variables, and the digest runs in a DIFFERENT Railway
        # service: meantime-concierge-digest holds no Stripe variables at
        # all (read back from Railway, 2026-09-14), so the same check there
        # would report every venue unverifiable every night. An env answer
        # is only true about the process that answers it.
        stripe_webhook_ready = all(
            stripe_integration.webhook_secret_configured_for(venue)
            for venue in venues
            if stripe_integration.is_configured_for(venue)
        )
        # Can every venue actually serve a client? Unfilled client-facing
        # columns (no fallback exists for any of them on purpose, so they
        # print blank on documents), plus the two spaces without which the
        # venue is worse than blank -- a venue with no "Unassigned (pending
        # triage)" space serves its enquiry FORM as a 200 and 500s on the
        # submit, losing the lead with no booking and no notification.
        # Proven by running it, 2026-09-14. See app/services/venue_readiness.
        #
        # All of it was already knowable and none of it was being asked at
        # runtime: `python -m app.seed` said it once, at deploy, into a log
        # line. A second venue's row is typed in by hand, days later.
        #
        # A BOOLEAN ONLY, detail logged: same rule as schema_drift above,
        # and what a named company is missing is not a fact a public
        # monitoring URL hands out. Aaron reads the gaps by name in the
        # 20:30 digest, off the same check.
        readiness = [venue_readiness.check(db, v) for v in venues]
        venues_ready = all(r.is_ready for r in readiness)
        for r in readiness:
            if not r.is_ready:
                logger.warning(
                    "venue %s is not ready: %d gap(s) -- %s",
                    r.slug, len(r.gaps), ", ".join(r.gaps),
                )
        checks = {
            "database": True,
            "venues_present": True,
            "gmail_configured": is_gmail_smtp_configured(),
            # Per venue, folded: true only when EVERY venue could actually
            # mint a payment link. A process-level "some key is set" answer
            # would read green while a second company had no key at all.
            # is_configured_for only reads an environment variable -- never
            # assert_key_belongs_to, which calls Stripe over the network and
            # has no business inside a health check.
            "stripe_configured": all(
                stripe_integration.is_configured_for(venue) for venue in venues
            ),
            "enquiry_notifications_failing": notification_failures > 0,
            # The AI drafting credential. Its failure branch wrote one row
            # and no log line at all, so an HTTP 401 was visible only as a
            # badge on a staff page nobody watches. Same shape as the
            # notification signal beside it: a count that flips the
            # endpoint, so a monitor sees it.
            "ai_drafting_failing": drafting_failures > 0,
            # Does the database carry what the migrations promised? Twice on
            # 2026-09-14 it did not, both times because a fix was made by
            # editing a migration that had already run -- so alembic read the
            # database as current, skipped the file, and nothing ever asked
            # the database itself. A passing test and a reviewed diff are not
            # evidence of applied state.
            #
            # A BOOLEAN ONLY. The detail is logged, because this endpoint is
            # public and a list of the triggers a database is missing is a map
            # of what is unguarded.
            "schema_drift": schema_drifting,
            "stripe_account_pinned": stripe_account_pinned,
            "stripe_webhook_ready": stripe_webhook_ready,
            "venues_ready": venues_ready,
            # THE AI GATES, reported because they are now a DELIBERATE
            # long-lived state rather than a transient one: Aaron, 2026-09-14,
            # "keep AI draft off for both venues, it's something we can work
            # on in a few months". A switch meant to stay off for months is
            # exactly the kind that comes back on without anyone noticing --
            # a database restore, a self-heal, a future session.
            #
            # They are SINGLETON and global, not per-venue: one ai_settings
            # row gates both companies, so this is the whole answer rather
            # than Hamilton's half of it.
            #
            # Reported, NOT degraded. A gate being open is a decision, not a
            # fault, and an endpoint that sits amber over a deliberate state
            # is one people stop reading. What this buys is that a change is
            # visible on the page already being watched.
            **ai_gates,
        }
        status = (
            "degraded"
            if (
                notification_failures > 0
                or drafting_failures > 0
                or schema_drifting
                or not stripe_account_pinned
                or not stripe_webhook_ready
                # DEGRADES, unlike the gates below it. An unfilled column is
                # not a decision anybody made; it is a document that will go
                # out wrong, or a venue that cannot take a booking.
                or not venues_ready
            )
            else "ok"
        )
    except Exception:
        # A real DB connection but something else broke -- still report
        # what we could confirm rather than raising a 500 for a monitor
        # to interpret however it likes.
        checks = {"database": True}
        status = "degraded"

    return {"status": status, "checks": checks, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
