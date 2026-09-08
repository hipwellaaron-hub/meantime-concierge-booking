"""Typed-but-unsaved Event Order content must not vanish on a click.

Everything on the Event Order edit page is typed into ONE form, and the
page carries other controls that navigate away: a Cancel link beside the
Save button, and a "Confirm <vendor> bump-in" submit button for a
DIFFERENT form rendered just below it. Clicking any of them discards
every unsaved change without a word.

This is what happened on HAM-20260911-AKPSO on 2026-09-08. The audit
trail records `vendor_bump_in_confirmed` at 00:14:00 and, one second
later, the `document_edited` that the bump-in handler's own vendor
refresh writes -- and no other document_edited at all. A save was never
submitted. The Event Order went out at 00:14:11 carrying the wizard's
generated values, and because several fields print a placeholder when
empty ("To be confirmed - contact the venue" for room layout, the
generated "43 adults, 0 kids" for special notes, no section at all for
decorations), the result was indistinguishable from content nobody had
ever entered.

These tests pin the markup and the wiring, which is what a server-side
test can honestly check about a browser guard: that the form is
identifiable, that the guard is bound to THAT form, that submitting it
clears the flag so saving never prompts, and that the separate actions
below it say they are separate.
"""

import datetime as dt
import re

import pytest

from app.models import BookingVendor, Contact
from app.models.document import DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_beo_content


def _booking(db, space, name="Guard Test"):
    contact = Contact(name="Guard Client", email=f"guard.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=43, child_count=0, notes=None, actor="test",
    )


def _edit_page(admin_client, db, space, *, with_unconfirmed_bump_in=False, name="Guard Test"):
    booking = _booking(db, space, name)
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, generate_beo_content(booking), actor="staff:test"
    )
    if with_unconfirmed_bump_in:
        db.add(
            BookingVendor(
                booking_id=booking.id, vendor_type="dj", name="DJ Matt Shepard",
                contact_number="0400 000 000", bump_in_time=dt.time(17, 30),
                bump_in_confirmed=False, source="staff",
            )
        )
        db.flush()
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    assert page.status_code == 200, page.status_code
    return booking, document, page.text


def test_the_edit_form_can_be_told_from_the_other_forms_on_the_page(admin_client, db, loft):
    """The guard has to bind to the edit form specifically. There are at
    least two other forms on this page and one of them is a submit button
    sitting right under Save."""
    _, _, html = _edit_page(admin_client, db, loft)

    assert 'id="beo-edit-form"' in html


def test_a_guard_is_armed_against_leaving_with_unsaved_changes(admin_client, db, loft):
    _, _, html = _edit_page(admin_client, db, loft)

    assert "beforeunload" in html, "nothing warns before the page is left"
    assert re.search(r'getElementById\("beo-edit-form"\)', html), "the guard is not bound to the edit form"


def test_saving_does_not_prompt(admin_client, db, loft):
    """A guard that fires on the Save button would train staff to click
    through it, which is worse than no guard."""
    _, _, html = _edit_page(admin_client, db, loft)

    guard = html[html.index('getElementById("beo-edit-form")'):]
    assert re.search(r'addEventListener\("submit",\s*function\s*\(\)\s*{\s*dirty = false', guard), (
        "submitting the form does not clear the dirty flag"
    )


def test_the_dirty_flag_is_set_by_editing_anything(admin_client, db, loft):
    """Both events, because a textarea fires input and a select fires
    change -- the vendor type and the AV checkboxes are the second kind."""
    _, _, html = _edit_page(admin_client, db, loft)

    guard = html[html.index('getElementById("beo-edit-form")'):]
    assert 'addEventListener("input"' in guard
    assert 'addEventListener("change"' in guard


def test_the_bump_in_confirm_button_says_it_is_a_separate_action(admin_client, db, loft):
    """It is the control that actually cost a page of typed content. It
    stays -- it is the only way to confirm a bump-in -- but it no longer
    reads as part of the edit above it."""
    _, _, html = _edit_page(admin_client, db, loft, with_unconfirmed_bump_in=True, name="Guard Bump")

    assert "confirm-bump-in" in html, "the confirm action is still available"
    assert "Separate actions" in html
    assert "unsaved edits above" in html


def test_no_separate_actions_note_when_there_is_nothing_to_confirm(admin_client, db, loft):
    """It must not appear on every Event Order -- a warning that is always
    there is furniture, and staff stop reading it."""
    _, _, html = _edit_page(admin_client, db, loft, name="Guard Quiet")

    assert "Separate actions" not in html
