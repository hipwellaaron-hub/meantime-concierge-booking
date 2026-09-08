"""Refusing a save that was decided against values which have since moved.

The edit form is rendered from a GET that may be minutes old. Nothing about
a row lock taken during the save reaches back that far, so without a
compare-and-set a staff member silently reverts whatever somebody else
committed while their page sat open -- and since the writer now records
authorship, the reverted text is marked as the reverting person's own words
and a regenerate preserves it. A lost update becomes a protected one.

The refusal must not itself destroy work. Returning a bare error would
protect one person's writing by throwing another's away, so the save comes
back as the form itself, carrying what they typed, a fresh fingerprint, and
a table of what moved. Then they decide.
"""

import re

import pytest
from sqlalchemy import event

from app.models.document import DocumentType
from app.services import content_authorship as ca
from app.services import document_regeneration, documents as documents_service
from app.services.document_generation import generate_agreement_content, generate_beo_content

PROTECTED = document_regeneration.PROTECTED_FIELD_NAMES


def _draft_beo(db, booking, content):
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")




def test_the_edit_screen_locks_before_it_reads_what_it_will_compare_against(db, booking):
    """The review's first finding. save_document_edit built its content dict
    from a read taken before the row was locked, so a value another
    transaction committed in between was both reverted (old, known,
    last-write-wins) AND recorded as the reverting staff member's own words
    -- turning a lost update into a protected one.

    Pinned at the service boundary: lock_draft_for_update must take the
    lock and hand back the CURRENT content, so what the caller reads next
    is what it is really replacing."""
    document = _draft_beo(db, booking, {"dietaries": "none"})

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        collapsed = " ".join(statement.split()).upper()
        if "FOR UPDATE" in collapsed and "DOCUMENTS" in collapsed:
            statements.append(collapsed)

    bind = db.get_bind()
    event.listen(bind, "after_cursor_execute", capture)
    try:
        locked = documents_service.lock_draft_for_update(db, document)
    finally:
        event.remove(bind, "after_cursor_execute", capture)

    assert statements, "no SELECT ... FOR UPDATE was issued, so nothing was locked"
    assert locked is document
    assert locked.content == {"dietaries": "none"}


def test_the_edit_screen_refuses_a_document_that_stopped_being_a_draft(db, booking):
    """The lock re-checks status, because the point of taking it early is
    that the earlier check was made against a version that may have moved:
    a document sent to the client between page load and save must not be
    edited in place."""
    document = _draft_beo(db, booking, {"dietaries": "none"})
    documents_service.mark_sent(db, document, actor="staff:test")

    with pytest.raises(ValueError, match="only a draft can be edited"):
        documents_service.lock_draft_for_update(db, document)


# --- the edit form refuses a save decided against values that have moved ------


def _renderable_beo(db, booking, overrides):
    """A BEO the edit template can actually render -- the generator's full
    shape, with the fields under test overlaid."""
    content = {**generate_beo_content(booking), **overrides}
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")


def _edit_form(admin_client, booking, document):
    """The hidden fields a browser would carry back, PLUS the prefilled
    value of every free-text field the form renders.

    Posting a partial form is not a faithful test: an omitted textarea
    arrives as empty, which the writer correctly reads as somebody clearing
    the field. A browser always re-posts what it was given."""
    import re

    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{document.id}/edit")
    assert page.status_code == 200
    prefilled = {
        name: document.content.get(name) or ""
        for name in PROTECTED
        if isinstance(document.content.get(name), str) or document.content.get(name) is None
    }
    return {
        **prefilled,
        "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1),
        "content_expect": re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1),
    }


def test_a_save_decided_against_stale_values_is_refused_not_silently_applied(db, booking, admin_client):
    """The review's first finding, end to end and through a real browser
    round trip. Staff A opens the edit screen; somebody else changes the
    dietaries; A saves with the value their page was rendered from. Before
    this, A's stale text overwrote the new one AND was recorded as A's own
    words, so a regenerate would then preserve it -- a lost update that had
    become a protected lost update.

    The row lock does not fix this and cannot: the staleness comes from the
    GET, not from a race inside the POST."""
    document = _renderable_beo(db, booking, {"dietaries": "none", "special_notes": "Rounds of 8."})
    form = _edit_form(admin_client, booking, document)

    # Somebody else records the allergy while A's page sits open.
    documents_service.update_content_fields(
        db,
        document,
        {"dietaries": "1x severe nut allergy (table 4)."},
        actor="staff:other",
        authored_fields=PROTECTED,
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={**form, "dietaries": "none", "special_notes": "Rounds of 8. Cake at 9pm."},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "changed while you had it open" in response.text
    db.refresh(document)
    assert document.content["dietaries"] == "1x severe nut allergy (table 4).", "the allergy is still there"
    assert ca.authored(document.content) == {"dietaries"}, "and still the other person's words"

    # The refusal hands their own work back rather than throwing it away: the
    # note they typed is in the form that returns, and the value they would
    # replace is named beside it.
    assert "Rounds of 8. Cake at 9pm." in response.text, "their typing survived the refusal"
    assert "1x severe nut allergy (table 4)." in response.text, "and they are told what they would replace"

    # Having been told, they can decide. Saving from the returned page carries
    # its fresh fingerprint and goes through -- refuse, explain, then let the
    # human choose, rather than deciding for them.
    fresh = re.search(r'name="content_expect" value="([^"]*)"', response.text).group(1)
    again = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={**form, "content_expect": fresh, "special_notes": "Rounds of 8. Cake at 9pm."},
        follow_redirects=False,
    )
    assert again.status_code == 303, "an informed overwrite is allowed -- it is their call"


def test_a_save_from_a_current_page_still_goes_through(admin_client, db, booking):
    """The guard must not turn every ordinary save into a conflict."""
    document = _renderable_beo(db, booking, {"dietaries": "none", "special_notes": "Rounds of 8."})
    form = _edit_form(admin_client, booking, document)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={**form, "dietaries": "1x severe nut allergy (table 4).", "special_notes": "Rounds of 8."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(document)
    assert ca.authored(document.content) == {"dietaries"}


def test_a_vendor_snapshot_refresh_does_not_make_an_open_edit_form_stale(db, booking, admin_client):
    """The fingerprint covers only the fields the form edits. A machine
    refresh of the vendor snapshot conflicts with nothing a person typed,
    and must not reject their save."""
    document = _renderable_beo(db, booking, {"dietaries": "none", "vendors": [{"name": "Old Florist"}]})
    form = _edit_form(admin_client, booking, document)

    # Exactly what confirm_vendor_bump_in does: the whole stored content
    # with the snapshot replaced. update_content takes a WHOLE dict, so
    # handing it a fragment would blank every other field.
    refreshed = {**document.content, "vendors": [{"name": "New Florist"}]}
    documents_service.update_content(db, document, refreshed, actor="staff:other")

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={**form, "dietaries": "1x severe nut allergy (table 4)."},
        follow_redirects=False,
    )

    assert response.status_code == 303, "a vendor refresh is not a conflicting edit"


def test_a_form_posted_without_the_token_is_refused_with_the_same_message(db, booking, admin_client):
    """A tab opened before this shipped posts no content_expect. It gets
    the explanation, not a bare 422 about a missing field."""
    document = _renderable_beo(db, booking, {"dietaries": "none"})
    form = _edit_form(admin_client, booking, document)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={"csrf_token": form["csrf_token"], "dietaries": "x"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "changed while you had it open" in response.text


def test_the_fingerprint_ignores_key_order_but_not_content(db, booking):
    """repr() of a dict depends on insertion order, so the same content
    built by two code paths could fingerprint differently and reject a save
    that conflicts with nothing -- and a spurious conflict is close to
    unreproducible. Canonical JSON settles it."""
    same_a = {"terms_sections": [{"heading": "Minimum spend", "body": "$2,000."}]}
    same_b = {"terms_sections": [{"body": "$2,000.", "heading": "Minimum spend"}]}
    different = {"terms_sections": [{"heading": "Minimum spend", "body": "$3,000."}]}

    fp = lambda c: documents_service.content_fingerprint(c, PROTECTED)  # noqa: E731
    assert fp(same_a) == fp(same_b), "key order is not a change"
    assert fp(same_a) != fp(different), "the words are"
    assert fp({**same_a, "vendors": [1]}) == fp({**same_a, "vendors": [2]}), "machine fields are not covered"


def test_a_document_sent_between_the_check_and_the_lock_is_a_409_not_a_500(
    db, booking, admin_client, monkeypatch
):
    """_get_draft_document_or_404 raises a clean 409 for a non-draft, and
    lock_draft_for_update raises ValueError for the same condition a
    microsecond later. Unhandled, that ValueError is a 500 -- the same
    situation reported two different ways depending on the timing.

    The window is real but too narrow to hit by arranging state, so it is
    forced: the document is sent DURING the lock call, which is exactly
    where the race lands. Sending it beforehand instead would be answered
    by the earlier check and would never reach the line under test -- the
    first version of this test did that and a mutation removing the fix
    passed it."""
    document = _renderable_beo(db, booking, {"dietaries": "none"})
    form = _edit_form(admin_client, booking, document)

    real_lock = documents_service.lock_draft_for_update

    def sent_while_locking(session, doc):
        documents_service.mark_sent(session, doc, actor="staff:other")
        return real_lock(session, doc)

    monkeypatch.setattr(documents_service, "lock_draft_for_update", sent_while_locking)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 409, "a 500 here means the ValueError escaped"
    assert "only a draft can be edited" in response.text


# --- the refusal must not destroy work either ---------------------------------


def test_a_field_the_staff_member_cleared_stays_cleared_on_the_conflict_page(db, booking, admin_client):
    """The conflict page overlaid only TRUTHY submitted values, so a field
    somebody had deliberately emptied fell through and the box came back
    holding the text they had just deleted. Saving again restored it, with
    nothing to say so -- the exact silent reversion this whole change exists
    to stop, reintroduced by the fix for it (review of 74e013c)."""
    document = _renderable_beo(db, booking, {"decorations": "Fairy lights, hired.", "dietaries": "none"})
    form = _edit_form(admin_client, booking, document)

    documents_service.update_content_fields(
        db, document, {"dietaries": "1x severe nut allergy (table 4)."}, actor="staff:other", authored_fields=PROTECTED
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={**form, "decorations": ""},
        follow_redirects=False,
    )

    assert response.status_code == 409
    box = re.search(r'name="decorations"[^>]*>(.*?)</textarea>', response.text, re.S)
    assert box is not None, "the decorations field should still be on the page"
    assert "Fairy lights" not in box.group(1), "their deletion was undone"
    assert box.group(1).strip() == "", "the box comes back as they left it: empty"


def test_the_conflict_table_names_only_fields_that_really_moved(db, booking, admin_client):
    """A raw != listed every multi-line field, because a browser posts CRLF
    where the stored value holds LF. A table that names untouched fields is
    no use for deciding what to discard, which is the one job it has."""
    stored_note = "Rounds of 8.\nCake at 9."
    reposted_note = "Rounds of 8.\r\nCake at 9."          # what a browser sends back
    document = _renderable_beo(db, booking, {"special_notes": stored_note, "dietaries": "none"})
    form = _edit_form(admin_client, booking, document)

    documents_service.update_content_fields(
        db, document, {"dietaries": "1x severe nut allergy (table 4)."}, actor="staff:other", authored_fields=PROTECTED
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data={**form, "special_notes": reposted_note},
        follow_redirects=False,
    )

    assert response.status_code == 409
    listed = re.findall(r"<td><strong>([^<]+)</strong></td>", response.text)
    assert listed == ["Dietaries"], f"only the field that moved should be listed, got {listed}"


# --- the agreement branch: hand-negotiated contract terms ---------------------


def test_an_agreement_conflict_re_renders_with_the_clause_they_typed(db, booking, admin_client):
    """The conflict handler builds its `submitted` dict two different ways --
    thirteen free-text fields for a BEO, and a terms_sections list rebuilt
    from the headings/bodies arrays for an agreement. Only the BEO path was
    covered, and the agreement one carries the hand-negotiated contract
    terms: the highest-value text in the system (review of 74e013c)."""
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    documents_service.update_content_fields(
        db,
        agreement,
        {"terms_sections": [{"heading": "Minimum spend", "body": "$9,000."}]},
        actor="staff:other",
        authored_fields=PROTECTED,
    )

    negotiated = "$2,000, as negotiated with the client."
    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit",
        data={
            "csrf_token": csrf,
            "content_expect": expect,
            "headings": ["Minimum spend"],
            "bodies": [negotiated],
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert negotiated in response.text, "their clause survived the refusal"
    assert "$9,000." in response.text, "and they are told what they would replace"
    db.refresh(agreement)
    assert agreement.content["terms_sections"][0]["body"] == "$9,000.", "nothing was written"


def test_an_agreement_save_from_a_current_page_still_goes_through(db, booking, admin_client):
    """The guard must not turn every ordinary agreement save into a conflict."""
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit",
        data={
            "csrf_token": csrf,
            "content_expect": expect,
            "headings": ["Minimum spend"],
            "bodies": ["$2,000, as negotiated with the client."],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(agreement)
    assert ca.authored(agreement.content) == {"terms_sections"}


# --- a refusal is never silent -----------------------------------------------


def test_writing_a_placeholder_over_somebody_s_real_text_is_named_not_hidden(db, booking, admin_client):
    """The worst failure this page has had. The rows were computed with
    changed_fields -- the AUTHORSHIP question -- which deliberately ignores
    a submission that writes the generator's own placeholder, because nobody
    authored it. But it certainly REPLACES what is there, and the whole
    banner was guarded on the row list being non-empty, so the refusal came
    back as a bare form with nothing said. It looked like the save had not
    taken; pressing save again swapped a colleague's negotiated text for
    "[REVIEW] add bar structure" and said nothing (review of 84916a7)."""
    placeholder = "[REVIEW] add bar structure"
    document = _renderable_beo(db, booking, {"bar_structure": placeholder, "dietaries": "none"})
    form = _edit_form(admin_client, booking, document)   # prefilled with the placeholder

    theirs = "Bar tab, $2,000 limit, agreed with the client."
    documents_service.update_content_fields(
        db, document, {"bar_structure": theirs}, actor="staff:other", authored_fields=PROTECTED
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "changed while you had it open" in response.text, "a refusal is never silent"
    listed = re.findall(r"<td><strong>([^<]+)</strong></td>", response.text)
    assert listed == ["Bar structure"], f"the field that would be overwritten must be named, got {listed}"
    assert theirs in response.text, "and their colleague's text shown, so the choice is informed"


def test_the_banner_still_explains_when_no_field_reads_differently(db, booking, admin_client):
    """The rows can legitimately come back empty. The fingerprint compares
    exact stored values; the table compares what the fields MEAN, and those
    are not the same test -- a field going from null to an empty string
    moves one and not the other.

    A 409 with no explanation is exactly what turned the placeholder bug
    above into lost text, so the banner is flagged separately from the row
    list and always says something."""
    document = _renderable_beo(db, booking, {"decorations": None, "dietaries": "none"})
    form = _edit_form(admin_client, booking, document)

    # null -> "": the same written value, a different stored one
    documents_service.update_content_fields(
        db, document, {"decorations": ""}, actor="staff:other", authored_fields=PROTECTED
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 409, "the fingerprint still refuses the save"
    assert "changed while you had it open" in response.text, "and never refuses silently"
    listed = re.findall(r"<td><strong>([^<]+)</strong></td>", response.text)
    assert listed == [], "nothing would actually read differently"
    assert "does not treat as a difference" in response.text, "so it says why the table is empty"


def test_an_agreement_conflict_shows_clauses_not_a_python_repr(db, booking, admin_client):
    """terms_sections is a list of dicts, and the table rendered it with
    str(), so the screen where somebody decides the fate of a
    hand-negotiated contract showed [{'body': '$9,000.', 'heading': ...}].
    Each field's own renderer is used instead -- the same one the regenerate
    screen shows these values through."""
    agreement = documents_service.create_new_version(
        db, booking, DocumentType.agreement, generate_agreement_content(booking), actor="test"
    )
    page = admin_client.get(f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    expect = re.search(r'name="content_expect" value="([^"]*)"', page.text).group(1)

    documents_service.update_content_fields(
        db,
        agreement,
        {"terms_sections": [{"heading": "Minimum spend", "body": "$9,000."}]},
        actor="staff:other",
        authored_fields=PROTECTED,
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{agreement.id}/edit",
        data={"csrf_token": csrf, "content_expect": expect,
              "headings": ["Minimum spend"], "bodies": ["$2,000, as negotiated."]},
        follow_redirects=False,
    )

    assert response.status_code == 409
    cells = re.findall(r'<td style="white-space:pre-wrap;">(.*?)</td>', response.text, re.S)
    assert cells, "the conflict table should have rendered"
    for cell in cells:
        assert "&#39;heading&#39;" not in cell and "{" not in cell, f"raw repr leaked into the table: {cell[:80]}"
    assert any("$9,000." in c for c in cells) and any("$2,000, as negotiated." in c for c in cells)


# --- what this check does NOT cover ------------------------------------------


def _beo_with_a_platter(db, booking, quantity=4):
    from decimal import Decimal

    content = generate_beo_content(
        booking,
        [{"item": "Antipasto Platter", "quantity": quantity, "unit_price": "85.00"}],
        deposit_paid=Decimal("0.00"),
    )
    content["dietaries"] = "none"
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")


def test_an_unchanged_food_order_is_not_named_on_the_conflict_screen(db, booking, admin_client):
    """The form posts a quantity as the string "4"; the document stores the
    int 4. Comparing the raw dicts made the food order read as moved on
    EVERY refusal, with identical text in both columns -- proved by running
    it. A warning that is always there is furniture, and this is the one
    screen whose whole job is telling somebody the truth about what they
    are about to overwrite."""
    document = _beo_with_a_platter(db, booking)
    form = _edit_form(admin_client, booking, document)
    form.update({
        "item_descriptions": ["Antipasto Platter"],
        "item_quantities": ["4"],
        "item_unit_prices": ["85.00"],
    })

    # Somebody else moves a DIFFERENT field, so the save is refused.
    documents_service.update_content_fields(
        db, document, {"dietaries": "1x severe nut allergy (table 4)."},
        actor="staff:other", authored_fields=PROTECTED,
    )

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 409
    listed = re.findall(r"<td><strong>([^<]+)</strong></td>", response.text)
    assert listed == ["Dietaries"], f"the food order did not move, yet it was named: {listed}"


def test_a_food_order_that_really_moved_is_still_named(db, booking, admin_client):
    """The other half, so the fix cannot be "stop looking at the food order".
    A quantity a colleague actually changed is still reported, and the two
    columns say different things."""
    document = _beo_with_a_platter(db, booking, quantity=4)
    form = _edit_form(admin_client, booking, document)
    form.update({
        "item_descriptions": ["Antipasto Platter"],
        "item_quantities": ["4"],
        "item_unit_prices": ["85.00"],
    })

    theirs = dict(document.content)
    theirs["food_order"] = {
        "line_items": [{"description": "Antipasto Platter", "quantity": 8, "unit_price": "85.00"}],
        "note": None,
    }
    documents_service.update_content(db, document, theirs, actor="staff:other", authored_fields=PROTECTED)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 409
    listed = re.findall(r"<td><strong>([^<]+)</strong></td>", response.text)
    assert listed == ["Food order"], f"the food order moved and was not named: {listed}"
    assert "8 x Antipasto Platter" in response.text, "their eight platters are not on the screen"
    assert "4 x Antipasto Platter" in response.text, "the four this form typed are not on the screen"


def test_a_category_only_change_is_refused_AND_explained(db, booking, admin_client):
    """The fingerprint compares the stored dict, so a colleague changing
    only a line's category refused the save -- and the renderer ignored
    category, so the table came back EMPTY under text saying none of the
    fields would read differently. Proved by running it.

    Refusing a save and then declining to say why is the exact shape of
    unhelpfulness this screen exists to end. And category is not cosmetic:
    document.html groups the client's line items by it.
    """
    document = _beo_with_a_platter(db, booking)
    form = _edit_form(admin_client, booking, document)
    form.update({
        "item_descriptions": ["Antipasto Platter"],
        "item_quantities": ["4"],
        "item_unit_prices": ["85.00"],
        "item_categories": [""],
    })

    theirs = dict(document.content)
    lines = [dict(line) for line in theirs["food_order"]["line_items"]]
    lines[0]["category"] = "platter"
    theirs["food_order"] = {"line_items": lines, "note": None}
    documents_service.update_content(db, document, theirs, actor="staff:other", authored_fields=PROTECTED)

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 409
    listed = re.findall(r"<td><strong>([^<]+)</strong></td>", response.text)
    assert listed == ["Food order"], f"refused with no reason given: {listed}"
    assert "(platter)" in response.text, "the heading their line sits under is not shown"
    assert "None of the fields on this form would read differently" not in response.text


def test_the_fingerprint_covers_the_food_order(db, booking, admin_client):
    """CLOSED, deliberately, by the commit that protected the food order.

    This was a characterisation test recording a known hole: the fingerprint
    covered the protected free-text fields only, so a colleague's change to
    the food order moved nothing the check looked at, the save was accepted,
    and their four platters were silently reverted with the total recomputed
    from the stale line.

    food_order is now a protected field, so the fingerprint covers it and
    this save refuses instead. Its own message asked whoever closed it to
    change the assertion on purpose rather than delete the test -- so the
    reversion check below is now the opposite claim: their four platters
    SURVIVE, and the refusal names Food order rather than refusing mutely.

    The rest of that old docstring still stands for the vendor rows, the key
    moments, the arrival time and the AV block. Those are still outside the
    fingerprint, and that is still last-write-wins."""
    from decimal import Decimal

    content = generate_beo_content(
        booking, [{"item": "Grazing Platter", "quantity": 1, "unit_price": "250.00"}],
        deposit_paid=Decimal("0.00"),
    )
    document = documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="test")
    form = _edit_form(admin_client, booking, document)
    form.update({
        "item_descriptions": ["Grazing Platter"],
        "item_quantities": ["1"],
        "item_unit_prices": ["250.00"],
    })

    # A colleague raises the quantity to four through their own form.
    theirs = dict(document.content)
    theirs["food_order"] = {
        "line_items": [{"description": "Grazing Platter", "quantity": 4, "unit_price": "250.00"}],
        "note": None,
    }
    documents_service.update_content(db, document, theirs, actor="staff:other", authored_fields=PROTECTED)
    db.refresh(document)
    assert document.content["food_order"]["line_items"][0]["quantity"] == 4

    response = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/{document.id}/edit",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 409, "the food order moved and the save went through anyway"
    db.refresh(document)
    assert document.content["food_order"]["line_items"][0]["quantity"] == 4, (
        "their four platters survived -- the save was refused, not applied"
    )
    # The refusal has to SAY what moved, or it is a 409 that teaches nobody.
    # Checked inside the conflict TABLE, not anywhere on the page: "Food
    # order" is also a heading on the edit form below it, so a bare
    # substring check passes whether or not the table names anything.
    table = response.text[response.text.index("Saving would put"):]
    assert "<strong>Food order</strong>" in table, table[:400]
