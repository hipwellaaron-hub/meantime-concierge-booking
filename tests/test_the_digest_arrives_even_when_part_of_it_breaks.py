"""A broken section must not silence the whole digest, and must not read as "none".

The digest is the channel. Aaron, 2026-09-14: "The nightly reconciliation at
20:30 and the digest after it are now how I find out something's wrong."

Until the same day, `build_digest` was five expressions inside one
constructor call and `send_digest.main` had no guard at all. So one
exception anywhere -- a reconciliation query, an invoice with an
unexpected shape, an SMTP error on the first recipient -- ended the run.
Every venue lost its email, every later recipient lost theirs, and the
cron simply failed. A digest that does not arrive is indistinguishable
from a quiet night, which is the wrong way round for the one thing he
relies on to find out something is wrong.

TWO PROPERTIES, and the second is the one that matters:

  * the email still goes;
  * a section that failed is NAMED and COUNTED, so an empty heading is
    never read as "nothing to do" when the truth is "nobody could look".

Every probe forces a real exception through a real section rather than
constructing a DigestContent by hand -- the failure being caught has to be
the one that actually happens, not one shaped like it.
"""
from unittest.mock import patch

import pytest

from app.services import digest, reconciliation, venue_readiness


class Boom(RuntimeError):
    pass


def test_a_failing_section_does_not_take_the_digest_down(db, hamilton):
    with patch.object(reconciliation, "open_findings", side_effect=Boom("db went away")):
        content = digest.build_digest(db, hamilton)

    assert content.failed_sections == ["Reconciliation"]


def test_the_other_sections_still_report(db, hamilton, booking):
    """The point of per-section rather than per-venue: one broken query
    must not cost the three answers that were available."""
    booking.event_date = None
    db.flush()

    with patch.object(reconciliation, "open_findings", side_effect=Boom("db went away")):
        content = digest.build_digest(db, hamilton)
        _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert content.failed_sections == ["Reconciliation"]
    # The venue set-up section was still computed, which proves the builder
    # carried on past the failure rather than short-circuiting.
    assert content.venue_gaps == []
    assert "COULD NOT BE CHECKED" in body


def test_the_failure_is_named_in_the_email(db, hamilton):
    with patch.object(reconciliation, "open_findings", side_effect=Boom("db went away")):
        content = digest.build_digest(db, hamilton)

    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "Reconciliation" in body
    assert "unknown, not empty" in body, (
        "the email names a failed section without saying that its emptiness "
        "means nothing"
    )


def test_a_failed_section_is_never_an_all_clear(db, hamilton):
    """THE one. Everything else is presentation; this is the difference
    between an email that lies and an email that does not."""
    with patch.object(reconciliation, "open_findings", side_effect=Boom("db went away")):
        content = digest.build_digest(db, hamilton)

    subject, _ = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert content.is_empty is False
    assert content.item_count >= 1
    assert "all clear" not in subject


def test_a_healthy_digest_says_nothing_about_failures(db, hamilton):
    """The positive control. Without it every probe here could be passing
    on a builder that reports a failure unconditionally."""
    content = digest.build_digest(db, hamilton)
    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert content.failed_sections == []
    assert "COULD NOT BE CHECKED" not in body


def test_the_failure_block_comes_first(db, hamilton):
    """Above the set-up gaps, which are themselves above the bookings.
    Every line below is a claim about current state; this one says some of
    those claims could not be made."""
    hamilton.abn = None
    db.flush()

    with patch.object(reconciliation, "open_findings", side_effect=Boom("db went away")):
        content = digest.build_digest(db, hamilton)

    _, body = digest.render_digest_text(content, dashboard_base_url="https://x")

    assert "COULD NOT BE CHECKED" in body and "SET-UP INCOMPLETE" in body
    assert body.index("COULD NOT BE CHECKED") < body.index("SET-UP INCOMPLETE")


@pytest.mark.parametrize(
    "target,label",
    [
        ("wizard", "Ready for the guided wizard"),
        ("overdue", "Overdue invoices"),
        ("readiness", "Venue set-up"),
    ],
)
def test_every_section_is_guarded_not_just_the_first(db, hamilton, target, label):
    """A guard on one section is a guard on nothing -- the exception that
    actually happens will be in a different one."""
    # Patched where the NAME IS LOOKED UP, not where it is defined:
    # digest.py does `from app.services.wizard import
    # get_wizard_eligible_bookings`, so patching the wizard module changes
    # nothing the digest can see. The first draft of this probe did exactly
    # that and reported the section as healthy.
    patches = {
        "wizard": (digest, "get_wizard_eligible_bookings"),
        "overdue": (digest, "get_overdue_invoices"),
        "readiness": (venue_readiness, "check"),
    }
    module, name = patches[target]

    with patch.object(module, name, side_effect=Boom("db went away")):
        content = digest.build_digest(db, hamilton)

    assert content.failed_sections == [label]


# --- the send loop ------------------------------------------------------


def test_one_recipients_failure_does_not_stop_the_next(db, hamilton, monkeypatch, capsys):
    """Two recipients, the first one's send raising. The second must still
    get theirs, and the run must end non-zero so the cron shows red rather
    than a quiet success that sent nothing."""
    from decimal import Decimal

    from app import send_digest
    from app.models import Space, Venue
    from app.seed import CLIENT_FACING_COLUMNS, UNASSIGNED_SPACE_NAME

    other = Venue(name="Meantime The Entrance", slug="entrance")
    for column in CLIENT_FACING_COLUMNS:
        setattr(other, column, getattr(hamilton, column))
    other.reference_prefix = "ENT"
    other.digest_recipient_email = "second@example.com"
    db.add(other)
    db.flush()
    db.add(Space(
        venue_id=other.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.add(Space(
        venue_id=other.id, name=UNASSIGNED_SPACE_NAME, capacity=0,
        standard_min_adults=0, min_food_spend=Decimal("0"), is_bookable=False,
    ))
    hamilton.digest_recipient_email = "first@example.com"
    db.flush()

    sent = []

    def fake_send(subject, body, recipient=None):
        if recipient == "first@example.com":
            raise Boom("smtp said no")
        sent.append(recipient)

    monkeypatch.setattr(send_digest.notifications, "is_digest_email_configured", lambda: True)
    monkeypatch.setattr(send_digest.notifications, "send_digest_email", fake_send)
    monkeypatch.setattr(send_digest, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    with pytest.raises(SystemExit) as exc:
        send_digest.main()

    assert sent == ["second@example.com"], (
        "the second recipient lost their digest because the first one's send failed"
    )
    assert "first@example.com" in str(exc.value)
