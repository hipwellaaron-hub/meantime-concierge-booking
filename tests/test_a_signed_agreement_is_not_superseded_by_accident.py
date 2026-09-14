"""Regenerate stops silently unsigning a signed agreement.

Aaron, 2026-09-14: "McKenzi's agreement is still draft, her event is on the
20th, and Regenerate will do this to the next booking as easily as it did
to hers."

WHAT WAS WRONG. `revise()` refuses this outright -- "revising it would
supersede the contract the client agreed to. Issue a new agreement for them
to sign instead." That refusal was never carried into
`create_new_version`, which is what Regenerate calls. So Regenerate flipped
is_current on the signed row, wrote a fresh draft into the current slot,
and raised a review flag only afterwards. HAM-20260920-I0K8G lost its
signed agreement that way, with the deposit paid and the event nine days
out. Neither the route nor the page checked.

The same function ALREADY refuses to regenerate over a LEGACY signed record
-- "the signed original stands". The rule existed and covered one of the
two ways a signature can arrive.

NOT AN OUTRIGHT REFUSAL. Generate is the only way to issue a replacement
agreement; revise()'s own message tells you to. A flat refusal would make
that sentence false. It is an opt-in the CALLER must pass, and the only
caller that passes it is the route that has already shown the confirmation
screen and checked its fingerprint.

THE BROWSER confirm() IS NOT THE GUARD. It matches the one the Event Order
has and it asks the question before a page load, but it is client-side. The
guard is the service refusal plus the server-rendered screen behind it.
"""
import datetime as dt
import re

import pytest

from app.models.document import DocumentStatus, DocumentType
from app.services import documents as documents_service
from app.services.booking import create_booking
from app.services.document_generation import generate_agreement_content


def _booking(db, loft, contact, name="Signed Agreement"):
    booking = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=9), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    return booking


def _signed_agreement(db, booking, signer="McKenzi Mostyn"):
    content = generate_agreement_content(booking)
    doc = documents_service.create_new_version(
        db, booking, DocumentType.agreement, content, actor="test"
    )
    db.flush()
    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.flush()
    doc.status = DocumentStatus.signed
    doc.signer_name = signer
    doc.signed_at = dt.datetime.now(dt.timezone.utc)
    db.flush()
    return doc


# --- the guard --------------------------------------------------------------


def test_regenerating_over_a_signed_agreement_is_refused(db, hamilton, loft, contact):
    """THE one. McKenzi's shape exactly."""
    booking = _booking(db, loft, contact, name="ZZSIGNED Refuse")
    signed = _signed_agreement(db, booking)

    with pytest.raises(ValueError, match="signed"):
        documents_service.create_new_version(
            db, booking, DocumentType.agreement,
            generate_agreement_content(booking), actor="staff:test@meantime.com.au",
        )

    db.refresh(signed)
    assert signed.is_current, "the signed agreement was superseded anyway"
    assert signed.status == DocumentStatus.signed
    assert documents_service.get_current(db, booking.id, DocumentType.agreement).id == signed.id


def test_the_refusal_names_the_signer_so_the_message_means_something(db, hamilton, loft, contact):
    booking = _booking(db, loft, contact, name="ZZSIGNED Named")
    _signed_agreement(db, booking, signer="McKenzi Mostyn")

    with pytest.raises(ValueError) as exc:
        documents_service.create_new_version(
            db, booking, DocumentType.agreement,
            generate_agreement_content(booking), actor="test",
        )

    assert "McKenzi Mostyn" in str(exc.value)


def test_the_opt_in_still_allows_issuing_a_replacement(db, hamilton, loft, contact):
    """revise() tells you to "issue a new agreement for them to sign
    instead", so that path must remain open -- a guard that made its own
    advice impossible would be worse than none."""
    booking = _booking(db, loft, contact, name="ZZSIGNED OptIn")
    signed = _signed_agreement(db, booking)

    fresh = documents_service.create_new_version(
        db, booking, DocumentType.agreement,
        generate_agreement_content(booking), actor="test", supersede_signed=True,
    )
    db.flush()

    assert fresh.status == DocumentStatus.draft
    assert fresh.version == signed.version + 1
    db.refresh(signed)
    assert not signed.is_current
    assert signed.status == DocumentStatus.signed, "the signed version must survive intact"
    assert signed.signer_name == "McKenzi Mostyn"


def test_an_unsigned_agreement_regenerates_freely(db, hamilton, loft, contact):
    """The guard must not make ordinary work harder: a sent-but-unsigned
    agreement has always been regenerable and stays so."""
    booking = _booking(db, loft, contact, name="ZZUNSIGNED Free")
    content = generate_agreement_content(booking)
    doc = documents_service.create_new_version(db, booking, DocumentType.agreement, content, actor="test")
    db.flush()
    documents_service.mark_sent(db, doc, actor="staff:test@meantime.com.au")
    db.flush()

    fresh = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    assert fresh.version == doc.version + 1


def test_an_approved_event_order_is_untouched_by_this(db, hamilton, loft, contact):
    """The deliberate difference the codebase already documents: a contract
    is what the client agreed to and stands; an Event Order changes right
    up to the day. This guard is agreement-only and must stay that way."""
    from app.services import beo_proposals

    booking = _booking(db, loft, contact, name="ZZBEO Approved")
    beo = documents_service.create_new_version(
        db, booking, DocumentType.beo, beo_proposals.fresh_beo_content(db, booking), actor="test"
    )
    db.flush()
    documents_service.mark_sent(db, beo, actor="staff:test@meantime.com.au")
    db.flush()
    beo.status = DocumentStatus.signed
    beo.signer_name = "A Client"
    db.flush()

    fresh = documents_service.create_new_version(
        db, booking, DocumentType.beo, beo_proposals.fresh_beo_content(db, booking), actor="test"
    )
    assert fresh.version == beo.version + 1


# --- the screens ------------------------------------------------------------


def test_the_route_shows_the_confirmation_screen(admin_client, db, hamilton, loft, contact):
    """Server-side, so it cannot be clicked past with JavaScript off -- and
    it returns 409 rather than writing."""
    booking = _booking(db, loft, contact, name="ZZSCREEN Signed")
    signed = _signed_agreement(db, booking)

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = admin_client.post(
        f"/admin/hamilton/bookings/{booking.id}/documents/agreement/generate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert resp.status_code == 409, f"expected the confirmation screen, got {resp.status_code}"
    assert "This agreement has been signed" in resp.text
    assert "unsigned draft" in resp.text
    db.refresh(signed)
    assert signed.is_current, "the screen was shown but the write happened anyway"


def test_the_booking_page_asks_before_the_page_load(admin_client, db, hamilton, loft, contact):
    """The browser confirm, matching the Event Order's. Asserted on the
    rendered page because a confirm nothing renders asks nobody.

    THE FIRST VERSION OF THIS TEST WAS VACUOUS and the mutation caught it:
    it asserted `onsubmit="return confirm(` appeared on the page, which the
    EVENT ORDER's own confirm satisfies, and `McKenzi Mostyn`, which the
    documents table prints anyway. Removing the agreement's confirm
    entirely left it green. It is scoped to the agreement form now.
    """
    booking = _booking(db, loft, contact, name="ZZCONFIRM Signed")
    _signed_agreement(db, booking)

    page = admin_client.get(f"/admin/hamilton/bookings/{booking.id}", follow_redirects=True)
    assert page.status_code == 200

    marker = "documents/agreement/generate"
    assert marker in page.text, "the agreement generate form is not on the page"
    form_open = page.text.rindex("<form", 0, page.text.index(marker))
    agreement_form = page.text[form_open:page.text.index("</form>", form_open)]

    assert 'onsubmit="return confirm(' in agreement_form, (
        "the agreement's Generate button asks nothing before superseding a signed contract"
    )
    assert "supersedes the contract the client agreed to" in agreement_form
    assert "McKenzi Mostyn" in agreement_form
