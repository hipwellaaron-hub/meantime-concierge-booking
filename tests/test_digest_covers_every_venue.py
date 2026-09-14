"""The digest is the only scheduled job that reports across the business.

Until 2026-09-12 app/send_digest.py looked up `slug="hamilton"` and sent one
venue's worth, so a second company's overdue invoices and pending wizards
would have been invisible -- the exact shape of Aaron's standing rule that
jobs loop venues.

Aaron, 2026-09-12: one email with the venues sectioned, not one email each,
"since I read it on a phone first thing and two emails means one gets
skimmed". A realistic busy morning across two venues renders in 23 lines.
"""
import datetime as dt
from decimal import Decimal

from app.models import Space, Venue
from app.send_digest import _group_by_recipient
from app.services.digest import build_digest, render_combined_digest


def _second_venue(db, *, slug="entrance", recipient=None):
    """FULLY SET UP, deliberately. This fixture used to carry a trading name
    and a reference prefix and nothing else -- a venue that could not print
    its own bank details on an invoice. Since 2026-09-14 the digest reports
    that as its own section, which is the correct answer and made these
    tests fail: they are about whether every venue gets a SECTION and
    whether the subject counts across venues, not about a half-typed row.
    A fixture that is not a venue anybody would operate cannot stand in for
    one."""
    venue = Venue(
        name="The Entrance", slug=slug, trading_name="Meantime The Entrance",
        reference_prefix="ENT" if slug == "entrance" else slug[:3].upper(),
        digest_recipient_email=recipient,
        legal_name="Nice Try Events Pty Ltd", abn="00 000 000 000",
        address="The Entrance NSW", phone="02 0000 0000",
        contact_name="Aaron", contact_email=f"{slug}@example.test",
        bank_account_name="Nice Try Events Pty Ltd",
        bank_bsb="000-000", bank_account_number="00000000",
        trading_days=[2, 3, 4, 5, 6],
        stripe_secret_key_env=f"STRIPE_SECRET_KEY_{slug.upper()}",
        stripe_webhook_secret_env=f"STRIPE_WEBHOOK_SECRET_{slug.upper()}",
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


def test_every_venue_gets_a_section(db, hamilton):
    """The defect. A venue that exists must appear, even with nothing
    outstanding -- an absent section reads as "nothing to do" and "that venue
    was never checked" identically, and the second is the one that matters
    the morning after a venue is added and something fails to wire up."""
    other = _second_venue(db)

    per_venue = [(v, build_digest(db, v)) for v in (hamilton, other)]
    subject, body = render_combined_digest(per_venue, dashboard_base_url="https://example.test")

    assert "MEANTIME HAMILTON" in body
    assert "MEANTIME THE ENTRANCE" in body
    assert body.count("Nothing needs attention right now.") == 2


def test_one_venue_renders_exactly_as_it_always_did(db, hamilton):
    """No heading, no separator. A single-venue business must not get a new
    email format because a second venue became possible."""
    from app.services.digest import render_digest_text

    content = build_digest(db, hamilton)
    combined = render_combined_digest([(hamilton, content)], dashboard_base_url="https://example.test")
    single = render_digest_text(content, dashboard_base_url="https://example.test")

    assert combined == single
    assert "===" not in combined[1]


def test_venues_sharing_a_recipient_are_one_email(db, hamilton):
    """Both venues point at DIGEST_RECIPIENT_EMAIL today (both NULL), so they
    group together and Aaron gets one email with two sections."""
    _second_venue(db)

    groups = _group_by_recipient(db)

    assert len(groups) == 1, f"expected one email, got {len(groups)}: {list(groups)}"
    assert len(next(iter(groups.values()))) == 2


def test_a_venue_with_its_own_recipient_splits_off(db, hamilton):
    """And the day The Entrance's digest should go to somebody else, it
    splits with no code change here."""
    _second_venue(db, recipient="ruby@example.test")

    groups = _group_by_recipient(db)

    assert set(groups) == {None, "ruby@example.test"}
    assert [v.slug for v in groups["ruby@example.test"]] == ["entrance"]
    assert [v.slug for v in groups[None]] == ["hamilton"]


def test_the_sections_are_in_a_stable_order(db, hamilton):
    """A digest whose sections shuffle between mornings is one you have to
    read rather than scan."""
    _second_venue(db, slug="aaa-venue")

    first = [v.slug for v in next(iter(_group_by_recipient(db).values()))]
    second = [v.slug for v in next(iter(_group_by_recipient(db).values()))]

    assert first == second
    assert first == sorted(first, key=lambda s: {"aaa-venue": "The Entrance", "hamilton": "Hamilton"}[s])


def test_an_items_venue_is_never_attributed_to_the_other_section(db, hamilton, loft, contact):
    """The thing a mixed list would break. An overdue invoice belongs under
    its own venue's heading and nowhere else."""
    from app.services.booking import create_booking
    from app.services.invoicing import create_deposit_invoice, mark_sent

    other = _second_venue(db)
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id, event_date=dt.date(2027, 4, 10),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="ZZSENTINEL Hamilton Party",
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )
    invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 1, 1), actor="test")
    mark_sent(db, invoice, actor="test")
    db.flush()

    per_venue = [(v, build_digest(db, v)) for v in (hamilton, other)]
    _, body = render_combined_digest(per_venue, dashboard_base_url="https://example.test")

    hamilton_part, entrance_part = body.split("=== MEANTIME THE ENTRANCE")
    assert "ZZSENTINEL" in hamilton_part
    assert "ZZSENTINEL" not in entrance_part, "a Hamilton invoice appeared under The Entrance"


def test_the_subject_counts_across_every_venue(db, hamilton, loft, contact):
    """One number for the morning, not per venue -- the subject is what gets
    read on a lock screen.

    An item at EACH venue, deliberately. The first version of this test put
    one overdue invoice at one venue, where sum() and max() both give 1, and
    it passed with the total replaced by max() -- a subject that would have
    under-reported every morning both venues had work.
    """
    from app.services.booking import create_booking
    from app.services.invoicing import create_deposit_invoice, mark_sent

    other = _second_venue(db)
    other_space = other.spaces[0]

    for space, name in ((loft, "Hamilton Overdue"), (other_space, "Entrance Overdue")):
        booking = create_booking(
            db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 4, 10),
            start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
            event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
        )
        invoice = create_deposit_invoice(db, booking, due_date=dt.date(2026, 1, 1), actor="test")
        mark_sent(db, invoice, actor="test")
    db.flush()

    per_venue = [(v, build_digest(db, v)) for v in (hamilton, other)]
    subject, body = render_combined_digest(per_venue, dashboard_base_url="https://example.test")

    assert "2 items" in subject, f"subject under-reported across venues: {subject!r}"
    assert "Hamilton Overdue" in body
    assert "Entrance Overdue" in body


def test_the_links_point_at_the_host_the_operator_is_signed_in_to(db, hamilton):
    """The admin session cookie is host-only. A link to a different host --
    even one serving the same app -- arrives with no cookie and lands on the
    login screen, which makes a phone digest useless.

    DASHBOARD_BASE_URL is set on the web service but NOT on the separate cron
    service that sends the digest (verified against Railway, 2026-09-12), so
    the default in app/config.py is what these links actually use.
    """
    from app.config import settings

    assert settings.dashboard_base_url == "https://book.meantime.com.au"
    assert "railway.app" not in settings.dashboard_base_url
