"""The Event Order prints one Music section, so the guard asks one question.

document.html renders `content.music or content.music_entertainment` as a
single "Music" section: the merged field is the older spelling of the same
value, and it prints only while there is no split `music`.

The regenerate guard treated them as two independent fields, and on the one
document shape where they disagree it came out backwards. A booking whose
Event Order was written before the split holds the entertainment detail in
`music_entertainment`; a rebuild from the wizard carries a split `music`.
The guard then reported "Music & entertainment would be emptied", and
keeping it wrote the merged value back UNDERNEATH the new `music`, where
the template never looks. The confirmation screen, the regenerate audit
line and the wizard's outstanding items all said the words were kept. The
run sheet printed the other value.

So the legacy spelling is read as `music` before either side of the
comparison, and a kept `music` takes the merged field with it.

This repair existed on 2bbd23e and went away with the revert of that
commit. It is being re-done deliberately rather than rediscovered.
"""

import datetime as dt
import re

import pytest

from app.models import Contact
from app.models.booking import BookingStatus
from app.models.document import DocumentType
from app.services import document_regeneration, documents as documents_service
from app.services import wizard as wizard_service
from app.services.booking import change_status, create_booking
from app.services.document_generation import REVIEW, generate_beo_content

# The real wizard driver, not one written from memory: a helper invented
# here would exercise inputs the wizard never sends.
from tests.test_wizard_generation import _complete_all_steps, _pay_deposit

LEGACY = "Live band 8pm-11pm, then DJ. Sound limiter briefed."
MUSIC_PROMPT = f"{REVIEW} add music/entertainment detail"


def _booking(db, space, name="Music Section"):
    contact = Contact(name="Music Client", email=f"music.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 5, 14),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=40, child_count=0, notes=None, actor="test",
    )


def _legacy_beo(db, booking, merged=LEGACY):
    """An Event Order written before the music field was split: the detail
    lives in the merged field and there is no `music`."""
    content = generate_beo_content(booking)
    content["music"] = None
    content["music_entertainment"] = merged
    return documents_service.create_new_version(db, booking, DocumentType.beo, content, actor="staff:test")


def _printed_music(content):
    """What the Music section shows -- the template's own rule."""
    return content.get("music") or content.get("music_entertainment")


def _fresh_with_split_music(booking, music="Chill playlist, no live acts."):
    content = generate_beo_content(booking, music=music)
    assert content["music"] == music
    assert content["music_entertainment"] is None, "the generator drops the merged field once music is set"
    return content


# --- the question --------------------------------------------------------------


def test_one_loss_row_for_one_section(db, loft):
    booking = _booking(db, loft)
    document = _legacy_beo(db, booking)

    found = document_regeneration.losses(db, document, _fresh_with_split_music(booking))

    assert [loss.field for loss in found] == ["music"], [loss.field for loss in found]
    row = found[0]
    assert row.label == "Music"
    assert row.current == LEGACY, "the screen shows the words that are actually printing"
    assert row.incoming == "Chill playlist, no live acts.", "and what would replace them"


def test_it_is_not_reported_as_being_emptied(db, loft):
    """The specific untruth. `music_entertainment` does go to nothing, but
    the section does not: it is replaced, and a person deciding needs to
    see what replaces it."""
    booking = _booking(db, loft)
    document = _legacy_beo(db, booking)

    found = document_regeneration.losses(db, document, _fresh_with_split_music(booking))

    assert all(loss.incoming for loss in found), [(l.field, l.incoming) for l in found]
    assert not any(loss.field == "music_entertainment" for loss in found)


def test_the_modern_shape_asks_the_same_single_question(db, loft):
    """A document already using the split field. One row, same label --
    the reading must not depend on which spelling the document happens to
    use."""
    booking = _booking(db, loft)
    content = generate_beo_content(booking, music="Live band 8pm-11pm.")
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )

    found = document_regeneration.losses(db, document, _fresh_with_split_music(booking))

    assert [loss.field for loss in found] == ["music"]
    assert found[0].current == "Live band 8pm-11pm."


@pytest.mark.parametrize("merged", [MUSIC_PROMPT, "", "   ", None])
def test_a_merged_field_that_is_not_a_persons_words_is_left_alone(db, loft, merged):
    """The generator's own prompt and an empty field are not entertainment
    detail, so neither becomes a `music` value nor a question."""
    booking = _booking(db, loft, f"Not Words {merged!r}")
    document = _legacy_beo(db, booking, merged=merged)

    found = document_regeneration.losses(db, document, _fresh_with_split_music(booking))

    assert found == [], [(loss.field, loss.current) for loss in found]


def test_a_merged_value_sitting_behind_a_split_music_is_not_read_as_music(db, loft):
    """It does not print, so it is not what the Music question is about --
    the `music` row must show the value the run sheet shows."""
    booking = _booking(db, loft, "Dead Weight")
    content = generate_beo_content(booking, music="Live band 8pm-11pm.")
    content["music_entertainment"] = LEGACY  # stale, behind the split value
    document = documents_service.create_new_version(
        db, booking, DocumentType.beo, content, actor="staff:test"
    )

    found = document_regeneration.losses(db, document, _fresh_with_split_music(booking))

    music = next(loss for loss in found if loss.field == "music")
    assert music.current == "Live band 8pm-11pm."


# --- the answer ----------------------------------------------------------------


def test_keeping_it_keeps_what_prints(db, loft):
    booking = _booking(db, loft, "Keep Prints")
    document = _legacy_beo(db, booking)
    fresh = _fresh_with_split_music(booking)

    merged = document_regeneration.apply_choices(fresh, document, {"music"})

    assert _printed_music(merged) == LEGACY, "the kept words are what the Music section shows"
    assert merged["music"] == LEGACY, "carried on the key the template reads first"


def test_not_keeping_it_takes_the_rebuilt_value(db, loft):
    booking = _booking(db, loft, "Replace Prints")
    document = _legacy_beo(db, booking)
    fresh = _fresh_with_split_music(booking)

    merged = document_regeneration.apply_choices(fresh, document, set())

    assert _printed_music(merged) == "Chill playlist, no live acts."


def test_the_audit_line_names_the_section_it_kept(db, loft):
    booking = _booking(db, loft, "Audit Music")
    document = _legacy_beo(db, booking)
    found = document_regeneration.losses(db, document, _fresh_with_split_music(booking))

    assert document_regeneration.summarise(found, {"music"}) == "kept Music"


# --- through the wizard, which is where it bites --------------------------------


def test_the_wizard_keeps_the_legacy_detail_and_it_reaches_the_run_sheet(db, loft, menu_items):
    """The client submits the wizard with their own music answer. There is
    nobody to ask, so the person's words are kept -- and the outstanding
    item saying so has to be true of what prints."""
    booking = _booking(db, loft, "Wizard Music")
    change_status(db, booking, BookingStatus.confirmed, actor="test")
    _pay_deposit(db, booking)
    _legacy_beo(db, booking)

    session = wizard_service.get_or_create_session(db, booking, actor="test")
    _complete_all_steps(db, session, menu_items)
    # The client's own submit, which is the door with nobody behind it.
    session, result = wizard_service.submit_review(db, session, actor="test")

    beo = documents_service.get_current(db, booking.id, DocumentType.beo)
    assert _printed_music(beo.content) == LEGACY, "the kept detail never reached the Music section"
    assert any("Music kept from the previous Event Order" in item for item in result.outstanding_items), (
        result.outstanding_items
    )
    assert not any("Music & entertainment" in item for item in result.outstanding_items), (
        "the merged spelling is not a second thing to confirm"
    )


# --- through the staff screen ---------------------------------------------------


def test_the_confirmation_screen_shows_both_values(admin_client, db, loft):
    booking = _booking(db, loft, "Screen Music")
    _legacy_beo(db, booking)
    # A split `music` reaches the staff regenerate only from the booking's
    # own data, so drive the screen from a proposal-free rebuild and read
    # what it offers: the point is the row's wording, not its origin.
    page = admin_client.get(f"/admin/bookings/{booking.id}")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    shown = admin_client.post(
        f"/admin/bookings/{booking.id}/documents/beo/generate", data={"csrf_token": csrf}
    )

    assert shown.status_code == 409
    assert LEGACY in shown.text, "the words at risk are named"
    assert re.findall(r'name="keep" value="([^"]+)"', shown.text) == ["music"]


# --- the reading itself, where the exclusions live ------------------------------
#
# Both of these survived their mutation through losses(), because a
# promoted placeholder is skipped again further down and a promoted empty
# string is skipped as an empty field. That makes them invisible there and
# testable only here: the helper's job is to answer what the Music section
# holds, and it must not answer "[REVIEW] add music/entertainment detail".


@pytest.mark.parametrize(
    "content, expected",
    [
        ({"music": None, "music_entertainment": LEGACY}, LEGACY),
        ({"music": "", "music_entertainment": LEGACY}, LEGACY),
        ({"music_entertainment": LEGACY}, LEGACY),
    ],
)
def test_the_legacy_spelling_is_read_as_music(content, expected):
    read = document_regeneration.read_music_as_split(content)
    assert read["music"] == expected
    assert read["music_entertainment"] is None


@pytest.mark.parametrize(
    "merged",
    [MUSIC_PROMPT, f"  {MUSIC_PROMPT}", "", "   ", None, 42, {"note": "x"}],
)
def test_what_is_not_a_persons_words_is_never_promoted(merged):
    """A generated prompt, an empty field and a value that is not text at
    all stay exactly where they are."""
    content = {"music": None, "music_entertainment": merged}

    read = document_regeneration.read_music_as_split(content)

    assert read is content, f"{merged!r} was rewritten"


def test_a_split_music_is_never_overwritten_by_the_older_spelling():
    content = {"music": "Chill playlist", "music_entertainment": LEGACY}

    assert document_regeneration.read_music_as_split(content) is content


def test_the_reading_does_not_mutate_the_document_content():
    """losses() and apply_choices() both read a live Document.content --
    rewriting it in place would edit the row they were only inspecting."""
    content = {"music": None, "music_entertainment": LEGACY}

    document_regeneration.read_music_as_split(content)

    assert content == {"music": None, "music_entertainment": LEGACY}
