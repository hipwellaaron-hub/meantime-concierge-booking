"""An email is from, and to, the venue its booking belongs to.

`ENQUIRY_NOTIFICATION_RECIPIENT` was `policy.VENUE_CONTACT_EMAIL`: one
module constant, bound at import, deciding the To of four staff alerts and
the Reply-To of two CLIENT emails, with no booking and no venue anywhere in
the decision. For a booking at the second venue every staff alert went to
Meantime Pty Ltd's inbox -- a different legal entity -- and the client's own
resume email arrived From "Meantime Hamilton", signed "Aaron / Meantime
Hamilton", telling them to write to Hamilton.

WHY IT REFUSES INSTEAD OF RENDERING BLANK, which is the whole shape of the
fix. `EmailMessage["To"] = None` does not raise: it stores the literal
string "None", and Gmail then refuses the recipient, so at least something
fails. But `From = "None <address>"` sends perfectly happily -- the client
receives an email from "None", signed "None". There is no safe blank here,
so a venue nobody has finished setting up does not send at all.

The old tests could not have caught any of this: they compared
`msg["To"]` against the same constant the code read, which holds whatever
the constant says. A whole-suite mutation putting a second venue's name into
the identity reads left 2356 of 2356 tests passing.
"""
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.models import Space, Venue
from app.services import notifications
from app.services.booking import create_booking

HAMILTON_INBOX = "meantimehamilton@gmail.com"
ENTRANCE_INBOX = "hello@nicetry.example"


@pytest.fixture()
def entrance(db, hamilton):
    venue = Venue(
        name="The Entrance", slug="entrance", trading_name="Meantime The Entrance",
        legal_name="Nice Try Events Pty Ltd", contact_name="Ruby",
        contact_email=ENTRANCE_INBOX, reference_prefix="ENT", trading_days=[2, 3, 4, 5, 6],
    )
    db.add(venue)
    db.flush()
    db.add(Space(
        venue_id=venue.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    ))
    db.flush()
    return venue


@pytest.fixture()
def smtp():
    """Captures the message instead of sending it. The Gmail ACCOUNT stays
    Hamilton's, because there is one sending account for the whole process
    -- which is exactly why From and To must be told apart."""
    mock = MagicMock()
    mock.__enter__.return_value = mock
    with patch.object(notifications, "DIGEST_GMAIL_ADDRESS", HAMILTON_INBOX), \
         patch.object(notifications, "DIGEST_GMAIL_APP_PASSWORD", "fake"), \
         patch.object(notifications.smtplib, "SMTP_SSL", return_value=mock):
        yield mock


def _sent(smtp):
    return smtp.send_message.call_args.args[0]


def _booking_at(db, venue, name="Venue Mail"):
    booking = create_booking(
        db, space_id=venue.spaces[0].id, contact_id=None,
        event_date=dt.date.today() + dt.timedelta(days=30), start_time=dt.time(18, 0),
        end_time=dt.time(23, 0), event_name=name, event_type="birthday",
        adult_count=40, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


# --- the four staff alerts -------------------------------------------------


def test_an_enquiry_alert_goes_to_its_own_venues_inbox(db, hamilton, entrance, smtp):
    booking = _booking_at(db, entrance)

    notifications.send_enquiry_notification_email(booking, dashboard_base_url="https://x")

    assert _sent(smtp)["To"] == ENTRANCE_INBOX, (
        "a Nice Try Events enquiry was announced to Meantime Pty Ltd's inbox"
    )


def test_a_deposit_paid_alert_goes_to_the_company_that_took_the_money(
    db, hamilton, entrance, smtp
):
    """The money is in Nice Try Events' Stripe account. The alert saying so
    was going to the other company."""
    booking = _booking_at(db, entrance, name="Deposit Alert")

    notifications.send_deposit_paid_email(
        booking, amount=Decimal("500.00"), agreement_signed=True, now_confirmed=True,
        dashboard_base_url="https://x",
    )

    assert _sent(smtp)["To"] == ENTRANCE_INBOX


def test_hamiltons_alerts_still_reach_hamilton(db, hamilton, loft, entrance, smtp):
    """The other direction, so a change that sent everything to the newest
    venue could not pass the tests above on its own."""
    booking = _booking_at(db, hamilton, name="Hamilton Alert")

    notifications.send_enquiry_notification_email(booking, dashboard_base_url="https://x")

    assert _sent(smtp)["To"] == hamilton.contact_email


# --- the client's own email ------------------------------------------------


def test_the_resume_email_a_client_gets_is_from_their_own_venue(
    db, hamilton, entrance, contact, smtp
):
    """One of only two emails this system deliberately sends a CLIENT."""
    booking = _booking_at(db, entrance, name="Resume Email")
    booking.contact_id = contact.id
    db.flush()

    notifications.send_wizard_resume_email(
        booking, resume_url="https://x/w/abc", due_date_display="1 October",
    )

    message = _sent(smtp)
    assert "Meantime The Entrance" in message["From"]
    assert "Meantime Hamilton" not in message["From"]
    assert message["Reply-To"] == ENTRANCE_INBOX
    body = message.get_content()
    assert ENTRANCE_INBOX in body, "the client was given an address to write to"
    assert HAMILTON_INBOX not in body
    assert body.rstrip().endswith("Meantime The Entrance"), (
        f"the client's email is signed by the wrong venue: {body.rstrip()[-60:]!r}"
    )


# --- the refusal, which is the point ---------------------------------------


@pytest.mark.parametrize("missing", ["trading_name", "contact_name", "contact_email"])
def test_a_venue_with_no_identity_refuses_rather_than_sending_as_None(
    db, hamilton, entrance, contact, smtp, missing
):
    """THE design constraint, proven by the survey: From = "None <address>"
    is accepted by Gmail without complaint and the client receives an email
    from None signed None. Blank is not a safe answer here, so there isn't
    one."""
    setattr(entrance, missing, None)
    db.flush()
    booking = _booking_at(db, entrance, name=f"Missing {missing}")
    booking.contact_id = contact.id
    db.flush()

    with pytest.raises(notifications.VenueMailNotConfigured) as exc:
        notifications.send_wizard_resume_email(
            booking, resume_url="https://x/w/abc", due_date_display=None,
        )

    assert missing in str(exc.value)
    assert not smtp.send_message.called, "it sent anyway"


def test_the_refusal_names_the_venue_so_it_can_be_fixed(db, hamilton, entrance):
    entrance.contact_email = None
    db.flush()

    with pytest.raises(notifications.VenueMailNotConfigured) as exc:
        notifications.venue_mail(entrance)

    assert "entrance" in str(exc.value)
    assert "venue row" in str(exc.value)


# --- the floor welcome -----------------------------------------------------


def test_a_new_casual_is_welcomed_by_the_venue_they_work_at(db, hamilton, entrance, smtp):
    """It signed off as Hamilton and pointed at Hamilton's inbox, to a Nice
    Try Events employee on their first day."""
    notifications.send_floor_welcome_email(
        name="Ruby", email="ruby@example.com", floor_url="https://x/floor", venue=entrance,
    )

    message = _sent(smtp)
    assert "Meantime The Entrance" in message["From"]
    assert message["Reply-To"] == ENTRANCE_INBOX
    body = message.get_content()
    assert ENTRANCE_INBOX in body and HAMILTON_INBOX not in body


def test_the_welcome_email_states_this_venues_closed_days(db, hamilton, entrance, smtp):
    """It told every new casual that "Monday and Tuesday are closed" -- one
    venue's trading week, typed into an email sent to every venue's staff."""
    entrance.trading_days = [4, 5]  # Friday and Saturday only
    db.flush()

    notifications.send_floor_welcome_email(
        name="Ruby", email="ruby@example.com", floor_url="https://x/floor", venue=entrance,
    )

    body = _sent(smtp).get_content()
    assert "Monday and Tuesday are closed" not in body
    assert "closed Monday, Tuesday, Wednesday, Thursday and Sunday" in body


def test_a_venue_that_has_not_recorded_its_week_claims_none(db, hamilton, entrance, smtp):
    """NULL means nobody has said. The sentence keeps its shape and simply
    makes no claim, rather than asserting another venue's week."""
    entrance.trading_days = None
    db.flush()

    notifications.send_floor_welcome_email(
        name="Ruby", email="ruby@example.com", floor_url="https://x/floor", venue=entrance,
    )

    body = _sent(smtp).get_content()
    assert "the month at a glance." in body
    assert "closed" not in body.split("Calendar:")[1].split("\n")[0]


# --- the two non-email surfaces --------------------------------------------


def test_the_staff_preview_shows_the_address_the_send_would_use(db, hamilton, entrance):
    """The mislabelled page in its purest form: the preview renders under a
    band reading "Meantime The Entrance" over a title that says everything
    here belongs to this venue, with a To cell reading Hamilton's inbox."""
    from app.services.enquiry_classification import preview_enquiry_notification

    booking = _booking_at(db, entrance, name="Preview")

    recipient, _subject, _body, _url = preview_enquiry_notification(booking)

    assert recipient == ENTRANCE_INBOX


def test_the_sweep_no_module_constant_decides_an_address_any_more():
    """ENQUIRY_NOTIFICATION_RECIPIENT is gone. A constant with the right
    shape is how the next caller reaches for it again."""
    assert not hasattr(notifications, "ENQUIRY_NOTIFICATION_RECIPIENT")
