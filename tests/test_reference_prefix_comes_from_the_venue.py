"""The reference code is the one string a client quotes back on the phone.

Until 2026-09-12 `generate_reference_code` read `venue_slug: str = "HAM"` and
BOTH call sites passed nothing, so every booking at every venue was stamped
`HAM-` -- while `venues.reference_prefix` existed, was unique, and was read by
no application code at all.

A reference is never rewritten once a client holds one (the re-code path at
`booking.py:1025` only fires while the code still says TBD and nothing has
been sent), so a wrong prefix is permanent for that booking.
"""
import datetime as dt
from decimal import Decimal

import pytest

from app.models import Space, Venue
from app.services.booking import create_booking, generate_reference_code


def _venue_with(db, *, slug, prefix):
    v = Venue(name=f"Venue {slug}", slug=slug, reference_prefix=prefix)
    db.add(v)
    db.flush()
    space = Space(
        venue_id=v.id, name="Private Bar Function", capacity=80,
        standard_min_adults=40, min_food_spend=Decimal("1000"), is_bookable=True,
    )
    db.add(space)
    db.flush()
    return v, space


def _book(db, space, **kw):
    args = dict(
        space_id=space.id, contact_id=None, event_date=dt.date(2027, 4, 10),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name="Party",
        event_type="birthday", adult_count=50, child_count=0, notes=None,
        actor="test",
    )
    args.update(kw)
    return create_booking(db, **args)


def test_a_booking_takes_its_own_venues_prefix(db, hamilton):
    """The defect. A second venue's booking must not be stamped HAM-."""
    _, space = _venue_with(db, slug="entrance-prefix-test", prefix="ENT")

    booking = _book(db, space)

    assert booking.reference_code.startswith("ENT-"), booking.reference_code
    assert "HAM" not in booking.reference_code


def test_hamiltons_own_bookings_are_unchanged(db, hamilton, loft):
    """The other half, and the reason this was safe to do now: Hamilton's
    seeded prefix is "HAM", which is exactly what the deleted default was,
    so nothing about a Hamilton reference changes."""
    booking = _book(db, loft)

    assert booking.reference_code.startswith("HAM-")


def test_a_venue_with_no_prefix_refuses_rather_than_guessing(db, hamilton):
    """A booking is the first thing a new venue does. A default would stamp
    the other company's letters on it and nobody would find out until a
    client read it aloud, so an unset prefix is a refusal."""
    _, space = _venue_with(db, slug="no-prefix-venue", prefix=None)

    with pytest.raises(ValueError, match="reference_prefix"):
        _book(db, space)


def test_an_empty_prefix_is_also_a_refusal(db, hamilton):
    """"" and "  " are not somebody's answer -- they are a field nobody
    finished, and they would produce a reference starting with "-"."""
    _, space = _venue_with(db, slug="blank-prefix-venue", prefix="   ")

    with pytest.raises(ValueError, match="reference_prefix"):
        _book(db, space)


def test_the_generator_reads_the_venue_it_is_given(db, hamilton):
    """Directly, without a booking in the way -- so a failure here points at
    the generator rather than at create_booking."""
    other = Venue(name="Prefix Probe", slug="prefix-probe", reference_prefix="PRB")
    db.add(other)
    db.flush()

    code = generate_reference_code(db, dt.date(2027, 1, 2), other)

    assert code.startswith("PRB-20270102-")


def test_a_dateless_enquiry_still_says_TBD_with_the_right_prefix(db, hamilton):
    """The normal shape of a first enquiry. The date segment is TBD; the
    venue segment still has to be that venue's."""
    _, space = _venue_with(db, slug="tbd-prefix-venue", prefix="TBD2")

    booking = _book(db, space, event_date=None, start_time=None, end_time=None)

    assert booking.reference_code.startswith("TBD2-TBD-")
