"""Three rulings, 2026-09-15, each closing a question the code asked wrongly.

1. SET-UP ACCESS IS NOT PRE-FILLED. The wizard seeded the field with 14:00
   -- the standard -- and every submitted time is stored as a request
   pending confirmation (his 28 Aug rule, unchanged). So a client who never
   touched the field submitted a request they had not made, and the booking
   page showed "requested setup access at 14:00 (earlier than standard) --
   pending confirmation" with a Confirm button, over a time that IS the
   standard. Blank now means no request.

2. THE ENQUIRY EMAIL NO LONGER CARRIES booking.notes. The booking page
   badges that field "never shown to the client"; the email printed it
   under DETAILS, in a body quoted in FULL the moment Reply is pressed,
   because Reply-To is the client. Harmless at enquiry time -- the field
   holds derived facts then -- and reachable through the manual RESEND,
   after staff have typed into it believing the badge.

3. THE WIZARD HAS A "NO MUSIC" OPTION. The step rejects an empty
   selection, so a client with no music had no truthful way through it:
   claim a playlist, a DJ or a musician they are not having, or abandon
   the wizard -- and whatever they ticked printed on the run sheet. An
   explicit answer rather than an allowed silence, so "no music" and
   "nobody answered" stay different things.
"""
import datetime as dt

import pytest

from app.services import notifications, wizard
from app.services.wizard import MusicType
from app.services.wizard_generation import build_music_text


# --- 1. set-up access ---------------------------------------------------


def test_a_wizard_with_no_setup_time_records_no_request(db, booking):
    session = wizard.get_or_create_session(db, booking, actor="test")

    wizard.save_basics_step(
        db, session,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        food_service_time=dt.time(19, 0), setup_access_time=None,
        adult_count=40, child_count=0, actor="client",
    )

    assert booking.setup_access_time is None
    assert booking.setup_access_confirmed is None, (
        "a client who asked for nothing is waiting on a confirmation -- "
        "NULL is the tri-state's 'never requested', False is 'pending'"
    )


def test_a_wizard_that_does_ask_still_records_a_pending_request(db, booking):
    """The rule that does NOT change: a time given is a request, never a
    promise. This is also the control -- without it, a change that ignored
    the field entirely would pass the probe above."""
    session = wizard.get_or_create_session(db, booking, actor="test")

    warnings = wizard.save_basics_step(
        db, session,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        food_service_time=dt.time(19, 0), setup_access_time=dt.time(11, 0),
        adult_count=40, child_count=0, actor="client",
    )

    assert booking.setup_access_time == dt.time(11, 0)
    assert booking.setup_access_confirmed is False
    assert any(w.code == "setup_access_requires_confirmation" for w in warnings)


def test_the_standard_time_is_still_a_request_when_asked_for(db, booking):
    """2pm typed deliberately is still a request -- his 28 Aug rule. What
    changed is that nobody types it by accident any more."""
    session = wizard.get_or_create_session(db, booking, actor="test")

    warnings = wizard.save_basics_step(
        db, session,
        start_time=dt.time(18, 0), end_time=dt.time(23, 0),
        food_service_time=dt.time(19, 0), setup_access_time=dt.time(14, 0),
        adult_count=40, child_count=0, actor="client",
    )

    assert booking.setup_access_confirmed is False
    assert not any(w.code == "setup_access_requires_confirmation" for w in warnings), (
        "2pm is the standard and must not warn that it is earlier than itself"
    )


def test_the_wizard_no_longer_seeds_the_field():
    """The seed lived in the client-side state, so it needs its own probe:
    the server can accept None all day and still never see one."""
    import pathlib

    page = pathlib.Path("app/templates/wizard/wizard.html").read_text(encoding="utf-8")

    assert 'DATA.booking.setup_access_time || "14:00"' not in page, (
        "the wizard still pre-fills the standard time, so every client "
        "submits a set-up request they did not make"
    )
    assert 'id="f-setup-time" required' not in page, (
        "the field is still required, so leaving it blank is not possible"
    )


# --- 2. the enquiry email ----------------------------------------------


def test_the_enquiry_email_carries_no_internal_notes():
    from tests.test_notifications import _booking

    booking = _booking(notes="Ring Aaron first, this one haggles.")

    body = notifications.build_enquiry_notification_body(booking)

    assert "DETAILS" not in body
    assert "haggles" not in body


def test_the_enquiry_email_still_carries_what_the_client_wrote():
    """The control. The client's own words are a different field
    (enquiry_text) and are safe to quote back to them -- they wrote them."""
    from tests.test_notifications import _booking

    booking = _booking(enquiry_text="Wanting balloons please.")

    body = notifications.build_enquiry_notification_body(booking)

    assert "WHAT THEY WROTE" in body
    assert "Wanting balloons please." in body


# --- 3. no music --------------------------------------------------------


def test_no_music_is_a_saveable_answer(db, booking):
    session = wizard.get_or_create_session(db, booking, actor="test")

    wizard.save_music_step(
        db, session, music_types=[MusicType.none],
        notes=None, bump_in_notes=None, actor="client",
    )

    assert session.music_response["music_types"] == ["none"]


def test_no_music_prints_as_a_statement_not_a_blank():
    """The floor needs to know the silence is intended rather than that
    nobody filled the step in."""
    text = build_music_text({"music_types": ["none"]})

    assert text is not None
    assert "No music" in text
    assert "asked for none" in text


def test_no_music_cannot_be_combined_with_a_dj(db, booking):
    """Not an answer anybody means, and letting it through would put both
    lines on the run sheet."""
    session = wizard.get_or_create_session(db, booking, actor="test")

    with pytest.raises(ValueError) as exc:
        wizard.save_music_step(
            db, session, music_types=[MusicType.none, MusicType.dj],
            notes=None, bump_in_notes=None, actor="client",
        )

    assert "No music" in str(exc.value)


def test_an_empty_selection_is_still_refused(db, booking):
    """The rule that does NOT change. "No music" is an explicit answer, not
    a way to skip the step -- otherwise "none" and "nobody answered" become
    the same thing again, which is the fault this closes."""
    session = wizard.get_or_create_session(db, booking, actor="test")

    with pytest.raises(ValueError):
        wizard.save_music_step(
            db, session, music_types=[], notes=None, bump_in_notes=None, actor="client",
        )


def test_the_real_options_still_work(db, booking):
    """The control: a change that accepted only "none" would pass
    everything above."""
    session = wizard.get_or_create_session(db, booking, actor="test")

    wizard.save_music_step(
        db, session, music_types=[MusicType.own_playlist, MusicType.dj],
        notes=None, bump_in_notes=None, actor="client",
    )

    assert session.music_response["music_types"] == ["own_playlist", "dj"]
    text = build_music_text(session.music_response)
    assert "Spotify" in text and "DJ" in text
    assert "No music" not in text


def test_the_wizard_offers_the_option_and_keeps_it_exclusive():
    """Client-side, so it needs its own probe: the server can refuse the
    combination forever while the page still lets somebody build it."""
    import pathlib

    page = pathlib.Path("app/templates/wizard/wizard.html").read_text(encoding="utf-8")
    start = page.index("function renderMusic()")
    block = page[start:start + 4000]

    assert 'data-key="none"' in block, "the step does not offer No music"
    assert 'state.music.music_types = ["none"]' in block, (
        "picking No music does not clear the other selections, so a client "
        "can build an answer the server will refuse"
    )
