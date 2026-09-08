"""A gap and a lost value must not look the same on the page.

Preston's Room Layout printed "To be confirmed - contact the venue", which
is what the client copy prints when the field is EMPTY. Decorations printed
no section at all. Special Notes printed a generated default. Onsite Contact
printed the client's own name. Nothing anywhere said "nobody filled this
in", so a page of content that never reached the server was
indistinguishable from a document nobody had touched (2026-09-08).

The staff copy now names them. Three things make it work:

  - it is VALUE-based, not record-based. `_authored` does not exist on a
    freshly generated document, and its silence is never evidence -- almost
    every document on production predates the record. Asking it would
    answer "nobody wrote this" about all of them, which is the reasoning
    that got the first provenance design reverted;
  - it is computed at RENDER time, so it is true of every document that
    already exists without regenerating any of them;
  - it reaches the PDF. The gap WAS visible on the staff web preview all
    along; `staff` was forced false whenever `is_pdf` was true, so the file
    Aaron downloads and reads in the venue was the one designed to hide it.
    Aaron, 2026-09-08: "that's where I missed Preston's."
"""

import datetime as dt
import re

import pytest

from app.models import Contact
from app.models.document import DocumentType
from app.services import document_regeneration as dr
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import (
    NO_DIETARIES,
    generate_agreement_content,
    generate_beo_content,
)

TYPED_LAYOUT = "Rounds of 8, dance floor centre."


def _booking(db, space, name="Gaps"):
    contact = Contact(name="Gap Client", email=f"gaps.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=43, child_count=0, notes=None, actor="test",
    )


def _beo(db, booking, **overrides):
    content = generate_beo_content(booking)
    content.update(overrides)
    return documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )


# --- the helper ---------------------------------------------------------------


def test_a_freshly_generated_event_order_is_almost_all_gaps(db, loft):
    booking = _booking(db, loft)

    gaps = dr.unfilled_fields(generate_beo_content(booking))

    for expected in ("Room layout notes", "Decorations", "Special notes", "Onsite contact"):
        assert expected in gaps, (expected, gaps)


def test_a_filled_field_drops_off_the_list(db, loft):
    booking = _booking(db, loft)

    gaps = dr.unfilled_fields(generate_beo_content(booking) | {"room_layout_notes": TYPED_LAYOUT})

    assert "Room layout notes" not in gaps
    assert "Decorations" in gaps, "the others are untouched"


def test_the_generated_dietaries_sentence_is_not_a_gap(db, loft):
    """REVERSED on review, deliberately, and the reasoning is worth keeping.

    The first version of this test said "No dietary requirements declared"
    reads like a decision nobody made, and counted it as a gap. That is
    true of a client nobody asked -- but the same sentence is also what a
    client who declared none produces, and that is most clients. So the
    block appeared on an Event Order with all ten fields filled in, saying
    "Not filled in (1): Dietaries". Proved by running it.

    On nearly every document, that is furniture: staff stop reading the
    block, and it stops working for Room layout, Decorations, Special notes
    and Onsite contact -- the fields Preston actually lost, and the reason
    the block exists at all.

    The never-asked case is real and is NOT fixed here. It is generation's
    to fix, by writing a [REVIEW] prompt where there is no answer instead
    of a sentence that asserts one. Naming every document to catch some of
    them is not a smaller version of that fix.
    """
    booking = _booking(db, loft)
    content = generate_beo_content(booking)
    assert content["dietaries"] == NO_DIETARIES, "the generator still writes the sentence"

    assert "Dietaries" not in dr.unfilled_fields(content)


def test_an_emptied_dietaries_field_is_not_a_gap_either(db, loft):
    """REPLACES a test of mine that asserted the opposite, on two counts.

    It pinned a state no writer can produce -- generation writes
    `dietaries or NO_DIETARIES` and the edit form writes
    `dietaries.strip() or NO_DIETARIES`, so the stored value is never empty
    -- and its rationale was FALSE: it said an empty field "prints nothing
    at all". document.html supplies the sentence itself
    (`content.get("dietaries") or NO_DIETARIES`), so the page reads exactly
    the same either way. Proved by reading the template.

    Which makes the exemption field-wide, and saying so plainly is better
    than an exemption that looks narrow and is not. The Dietaries section
    cannot mislead anybody about whether it was filled in, in either
    direction, because it prints one sentence whatever is stored.
    """
    booking = _booking(db, loft)

    assert "Dietaries" not in dr.unfilled_fields(generate_beo_content(booking) | {"dietaries": ""})
    assert "Dietaries" not in dr.unfilled_fields(generate_beo_content(booking) | {"dietaries": None})


def test_a_review_prompt_is_still_a_gap(db, loft):
    """The other generated placeholders say out loud that nobody answered,
    so they stay gaps -- the exemption is one sentence, not the whole set."""
    booking = _booking(db, loft)

    gaps = dr.unfilled_fields(generate_beo_content(booking))

    assert "Bar structure" in gaps, "the [REVIEW] prompts are still gaps"
    assert "Catering order & service style" in gaps


def test_it_does_not_report_fields_the_document_does_not_have(db, loft):
    """An agreement is not missing ten Event Order fields, and an Event
    Order is not missing the agreement's terms."""
    booking = _booking(db, loft)

    beo_gaps = dr.unfilled_fields(generate_beo_content(booking))
    agreement_gaps = dr.unfilled_fields(generate_agreement_content(booking))

    assert "Agreement terms" not in beo_gaps
    assert "Room layout notes" not in agreement_gaps


def test_a_legacy_music_value_is_not_called_a_gap(db, loft):
    """The detail is in the merged field, which is what that document
    prints. Read it the way it prints."""
    booking = _booking(db, loft)
    content = generate_beo_content(booking)
    content["music"] = None
    content["music_entertainment"] = "Live band 8pm-11pm, then DJ."

    gaps = dr.unfilled_fields(content)

    assert "Music" not in gaps
    assert "Music & entertainment" not in gaps


def test_a_split_music_value_answers_for_the_whole_section(db, loft):
    """Both keys exist on a modern document; only one prints. The section
    is named once or not at all, never twice."""
    booking = _booking(db, loft)
    content = generate_beo_content(booking, music="Chill playlist")

    gaps = dr.unfilled_fields(content)

    assert "Music" not in gaps
    assert "Music & entertainment" not in gaps


def test_junk_content_does_not_raise(db, loft):
    """Reads tolerate anything -- a malformed row must not be the thing
    that breaks a document page."""
    assert dr.unfilled_fields(None) == []
    assert dr.unfilled_fields("not a dict") == []


# --- what the staff sees ------------------------------------------------------


def test_the_staff_preview_names_the_gaps(admin_client, db, loft):
    booking = _booking(db, loft, "Gaps Preview")
    document = _beo(db, booking)

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/preview")

    assert page.status_code == 200
    assert "Not filled in" in page.text
    assert "Room layout notes" in page.text
    assert "Decorations" in page.text, "the section that prints nothing at all"


def test_the_client_never_sees_the_block(client, db, loft):
    booking = _booking(db, loft, "Gaps Client")
    document = _beo(db, booking)
    documents_service.mark_sent(db, document, actor="staff:test")

    page = client.get(f"/d/{document.access_token}")

    assert page.status_code == 200
    assert "Not filled in" not in page.text
    assert "Staff copy only" not in page.text


def test_the_block_disappears_once_everything_is_filled_in(admin_client, db, loft):
    """A warning that is always there is furniture."""
    booking = _booking(db, loft, "Gaps None")
    content = generate_beo_content(booking, music="Chill playlist")
    for name in dr.PROTECTED_FIELD_NAMES:
        if name in content and name not in ("food_order", "terms_sections", "music_entertainment"):
            content[name] = f"filled in: {name}"
    content["food_order"] = {
        "line_items": [{"description": "Grazing Platter", "quantity": 2, "unit_price": "250.00"}],
        "note": None,
    }
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/preview")

    assert "Not filled in" not in page.text, dr.unfilled_fields(document.content)


# --- the PDF, which is the one that got read ----------------------------------


def test_the_staff_pdf_exists_and_is_named_internal(admin_client, db, loft):
    booking = _booking(db, loft, "Gaps Pdf")
    document = _beo(db, booking)

    resp = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "INTERNAL" in resp.headers["content-disposition"]
    assert resp.content[:4] == b"%PDF"


def _pdf_text(content: bytes) -> str:
    """The words actually in the file, not in the HTML it was made from."""
    import io

    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


def test_the_gaps_are_in_the_downloaded_pdf_itself(admin_client, client, db, loft):
    """Through the ROUTES and read back out of the PDF bytes, because the
    file is the thing Aaron reads on a phone in the venue and that is where
    Preston's gap was missed. Asserting on the HTML the route rendered
    would not catch a route that rendered the wrong one -- it did not, and
    a mutation proved the test blind before this existed.
    """
    booking = _booking(db, loft, "Gaps Real Pdf")
    document = _beo(db, booking)
    documents_service.mark_sent(db, document, actor="staff:test")

    staff_pdf = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/pdf")
    client_pdf = client.get(f"/d/{document.access_token}/pdf")
    assert staff_pdf.status_code == 200 and client_pdf.status_code == 200

    staff_text = _pdf_text(staff_pdf.content)
    client_text = _pdf_text(client_pdf.content)

    assert "Not filled in" in staff_text, staff_text[:400]
    assert "Room layout notes" in staff_text
    assert "Not filled in" not in client_text, "the client's own PDF is naming staff gaps"
    # And the marker the client copy replaces with a neutral sentence.
    assert "[REVIEW]" in staff_text
    assert "To be confirmed" in client_text


def test_the_staff_pdf_carries_the_gaps_and_the_client_pdf_does_not(admin_client, client, db, loft):
    """The whole point of the request. Compared as rendered HTML, because
    reading text back out of PDF bytes is its own problem -- the route is
    the same render either way, and the byte test above proves it produces
    a real PDF."""
    from app.templating import templates

    booking = _booking(db, loft, "Gaps Both")
    document = _beo(db, booking)

    staff_html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True, is_staff_preview=True
    )
    client_html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True
    )

    assert "Not filled in" in staff_html, "the staff PDF still hides the gaps"
    assert "Room layout notes" in staff_html
    assert "Not filled in" not in client_html, "the client PDF started showing staff content"


def test_the_staff_pdf_shows_the_review_marker_the_client_pdf_hides(db, loft):
    """The specific thing that made Preston's gap invisible: the client
    render swaps [REVIEW] for "To be confirmed - contact the venue"."""
    from app.templating import templates

    booking = _booking(db, loft, "Gaps Review")
    document = _beo(db, booking)

    staff_html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True, is_staff_preview=True
    )
    client_html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True
    )

    assert "[REVIEW] add room layout notes" in staff_html
    assert "To be confirmed" in client_html
    assert "[REVIEW]" not in client_html


def test_the_floor_apps_shared_pdf_stays_clean(db, loft):
    """It is explicitly the shareable copy -- 'a shared PDF can leave the
    team'. It never asked for staff content and must not start getting it."""
    from app.templating import templates

    booking = _booking(db, loft, "Gaps Floor")
    document = _beo(db, booking)

    floor_html = templates.get_template("document.html").render(
        document=document, booking=booking, is_pdf=True
    )

    assert "Not filled in" not in floor_html


# --- onsite contact -----------------------------------------------------------


def test_an_empty_onsite_contact_is_not_the_clients_name(admin_client, db, loft):
    """It printed the client's name, which reads as a decision rather than
    a gap -- and on the night that is the number somebody calls."""
    booking = _booking(db, loft, "Gaps Onsite")
    document = _beo(db, booking)
    assert document.content.get("onsite_contact") is None

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/preview")

    onsite = page.text[page.text.index("Onsite Contact:"):][:200]
    assert "Gap Client" not in onsite, "the client's name is standing in for a missing onsite contact"
    assert "Onsite contact" in page.text, "and it is named in the block"


def test_a_real_onsite_contact_prints_in_full_including_the_number(admin_client, db, loft):
    """The number is what the floor needs. person_name only recases
    all-lower or all-upper text; it never strips anything."""
    booking = _booking(db, loft, "Gaps Onsite Real")
    document = _beo(db, booking, onsite_contact="Sally Roberts 0400 111 222")

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/preview")

    assert "Sally Roberts 0400 111 222" in page.text
    assert "Onsite contact" not in page.text.split("Not filled in")[-1][:300] if "Not filled in" in page.text else True


def test_the_client_copy_still_falls_back_to_their_own_name(client, db, loft):
    """Unchanged for the client: on their copy naming them is reasonable,
    and this change is about staff being able to see a gap."""
    booking = _booking(db, loft, "Gaps Onsite Client")
    document = _beo(db, booking)
    documents_service.mark_sent(db, document, actor="staff:test")

    page = client.get(f"/d/{document.access_token}")

    onsite = page.text[page.text.index("Onsite Contact:"):][:200]
    assert "Gap Client" in onsite


@pytest.fixture()
def client(db, hamilton):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- and before you send it ---------------------------------------------------


def test_the_edit_form_names_the_gaps_before_you_send(admin_client, db, loft):
    """Aaron: "the admin shows it before you generate." Same helper, so the
    edit screen and the document can never disagree about what is missing."""
    booking = _booking(db, loft, "Gaps Form")
    document = _beo(db, booking)

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")

    assert page.status_code == 200
    assert "Not filled in as at the last save" in page.text
    assert "Room layout notes" in page.text
    assert "Decorations" in page.text


def test_a_save_that_lands_clears_the_field_from_the_list(admin_client, db, loft):
    """The Preston detection, stated as a test: save, come back, and if the
    field is STILL listed then the save did not land."""
    import re

    booking = _booking(db, loft, "Gaps Form Save")
    document = _beo(db, booking)
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]+)"', page.text).group(1)
    assert "Room layout notes" in page.text

    admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={"csrf_token": csrf, "content_expect": expect, "room_layout_notes": TYPED_LAYOUT},
        follow_redirects=False,
    )

    after = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    gaps_block = after.text[after.text.index("Not filled in as at the last save"):][:600]
    assert "Room layout notes" not in gaps_block, "the save landed but the list still says it did not"
    assert "Decorations" in gaps_block, "and the ones still empty are still named"


def test_the_form_says_the_list_is_the_stored_document(admin_client, db, loft):
    """It goes stale as soon as you type, so it has to say so -- otherwise
    it becomes the next thing that looks true and is not."""
    booking = _booking(db, loft, "Gaps Form Stale")
    document = _beo(db, booking)

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")

    assert "not the boxes below" in page.text
