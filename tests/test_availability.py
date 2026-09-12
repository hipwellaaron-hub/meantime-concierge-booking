import datetime as dt

from fastapi.testclient import TestClient

from app.main import app
from app.database import get_db
from app.models.booking import BookingStatus
from app.services.availability import get_space_candidates, is_space_free
from app.services.booking import create_booking

EVENT_DATE = dt.date(2026, 9, 12)  # a Saturday


def _book(db, space, start, end, **overrides):
    # Defaults to 'tentative' (a real hold) rather than 'enquiry', since
    # enquiries/offers deliberately don't block the space -- see
    # BLOCKING_STATUSES in app/models/booking.py.
    kwargs = dict(
        space_id=space.id,
        contact_id=None,
        event_date=EVENT_DATE,
        start_time=start,
        end_time=end,
        event_name="Test Event",
        event_type="party",
        adult_count=10,
        child_count=0,
        notes=None,
        actor="test",
        status=BookingStatus.tentative,
    )
    kwargs.update(overrides)
    return create_booking(db, **kwargs)


def test_is_space_free_true_with_no_bookings(db, loft):
    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True
    assert blocking == []


def test_is_space_free_false_once_booked(db, loft):
    _book(db, loft, dt.time(12, 0), dt.time(16, 0))
    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is False
    assert len(blocking) == 1


def test_enquiry_status_does_not_block_the_space(db, loft):
    _book(db, loft, dt.time(12, 0), dt.time(16, 0), status=BookingStatus.enquiry)
    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True
    assert blocking == []


def test_multiple_enquiries_can_coexist_for_the_same_slot(db, loft):
    """The whole point of lead capture: two people enquiring about the same
    date shouldn't stop the second enquiry from even being logged."""
    _book(db, loft, dt.time(12, 0), dt.time(16, 0), status=BookingStatus.enquiry, event_name="Enquiry A")
    _book(db, loft, dt.time(12, 0), dt.time(16, 0), status=BookingStatus.enquiry, event_name="Enquiry B")

    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True  # neither has actually secured the space yet


def test_cancelled_booking_does_not_block(db, loft):
    from app.models.booking import BookingStatus
    from app.services.booking import change_status

    booking = _book(db, loft, dt.time(12, 0), dt.time(16, 0))
    change_status(db, booking, BookingStatus.cancelled, actor="test")

    free, blocking = is_space_free(db, loft.id, EVENT_DATE)
    assert free is True
    assert blocking == []


def test_get_space_candidates_excludes_too_small(db, hamilton, lounge):
    # Lounge caps at 35; ask for 60 guests.
    candidates = get_space_candidates(db, hamilton.id, EVENT_DATE, dt.time(12, 0), dt.time(16, 0), guest_count=60)
    lounge_result = next(c for c in candidates if c["space"].id == lounge.id)
    assert lounge_result["is_available"] is False
    assert "too_small" in lounge_result["reasons"]


def test_get_space_candidates_excludes_already_booked(db, hamilton, loft):
    _book(db, loft, dt.time(12, 0), dt.time(16, 0))

    candidates = get_space_candidates(db, hamilton.id, EVENT_DATE, dt.time(14, 0), dt.time(18, 0), guest_count=10)
    loft_result = next(c for c in candidates if c["space"].id == loft.id)
    assert loft_result["is_available"] is False
    assert "already_booked" in loft_result["reasons"]


def test_get_space_candidates_non_overlapping_time_is_available(db, hamilton, loft):
    _book(db, loft, dt.time(9, 0), dt.time(12, 0))

    candidates = get_space_candidates(db, hamilton.id, EVENT_DATE, dt.time(13, 0), dt.time(17, 0), guest_count=10)
    loft_result = next(c for c in candidates if c["space"].id == loft.id)
    assert loft_result["is_available"] is True
    assert loft_result["reasons"] == []


def test_wheelchair_filter_excludes_inaccessible_spaces(db, hamilton, loft, lounge):
    candidates = get_space_candidates(
        db, hamilton.id, EVENT_DATE, dt.time(12, 0), dt.time(16, 0), guest_count=10, require_wheelchair_accessible=True
    )
    loft_result = next(c for c in candidates if c["space"].id == loft.id)
    lounge_result = next(c for c in candidates if c["space"].id == lounge.id)
    assert "not_accessible" in loft_result["reasons"]
    assert lounge_result["is_available"] is True


def test_spaces_endpoint_returns_warnings_for_saturday_daytime_overrun(db, hamilton):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/availability/spaces",
            # venue_slug is REQUIRED now: this endpoint is public and a
            # default meant an unspecified caller got Hamilton's rooms and
            # minimum spends as though they had asked for them.
            params={"date": str(EVENT_DATE), "start": "11:00:00", "end": "18:00:00",
                    "guests": 20, "venue_slug": hamilton.slug},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert any("4:30pm" in w for w in body["warnings"])
        assert len(body["spaces"]) == 3
    finally:
        app.dependency_overrides.clear()


def test_availability_endpoint_reports_free_space(db, hamilton, loft):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(loft.id)})
        assert resp.status_code == 200
        assert resp.json()["is_free"] is True
    finally:
        app.dependency_overrides.clear()


def test_availability_endpoint_handles_blocking_booking_with_null_times(db, hamilton, unassigned_space):
    """Regression: a migrated 'confirmed' booking can have NULL start/end
    time. Serializing it into the availability response used to crash
    with a pydantic ValidationError (start_time/end_time were declared
    non-optional) instead of returning it as a blocking booking with
    unknown times."""
    _book(
        db,
        unassigned_space,
        start=None,
        end=None,
        status=BookingStatus.confirmed,
        event_name="Migrated booking",
    )

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(unassigned_space.id)})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404  # unassigned_space is not bookable -- see next test for why


def test_availability_endpoint_rejects_non_bookable_space(db, hamilton, unassigned_space):
    """The internal migration-triage placeholder space must never be
    queryable as if it were a real bookable space."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(unassigned_space.id)})
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_is_space_free_serializes_null_time_blocking_booking_via_real_space(db, hamilton, loft):
    """Same NULL-time crash, but through a real bookable space (a
    'tentative'/'confirmed' booking can legitimately have no time set
    yet), so the 200 response path itself is exercised end-to-end."""
    _book(db, loft, start=None, end=None, status=BookingStatus.confirmed, event_name="No time yet")

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/availability", params={"date": str(EVENT_DATE), "space_id": str(loft.id)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_free"] is False
        assert body["blocking_bookings"][0]["start_time"] is None
        assert body["blocking_bookings"][0]["end_time"] is None
    finally:
        app.dependency_overrides.clear()


# --- the public endpoint cannot pick a venue for you ------------------------
#
# Aaron, 2026-09-12, on this specifically: "that's the call I make dozens of
# times a day and a silent default to Hamilton is how I'd confidently quote
# the wrong building." Closed rather than moved: the parameter is required,
# not defaulted somewhere else.


def _probe(db, **params):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        base = {"date": str(EVENT_DATE), "start": "18:00:00", "end": "23:00:00", "guests": 20}
        base.update(params)
        return TestClient(app).get("/availability/spaces", params=base)
    finally:
        app.dependency_overrides.clear()


def test_omitting_the_venue_is_refused_rather_than_defaulted(db, hamilton):
    """THE guard. A caller who says nothing used to get Hamilton's rooms,
    capacities and minimum food spends -- a quote in all but name -- with
    nothing to say a venue had been chosen for them."""
    resp = _probe(db)

    assert resp.status_code == 422, (
        f"a venue-less request was answered with {resp.status_code}; it must be refused"
    )
    assert "venue_slug" in resp.text, "the refusal does not name what is missing"


def test_an_empty_venue_is_refused_too(db, hamilton):
    """"" is not a venue. Without min_length it would pass validation and
    then 404 as "Unknown venue ''", which reads like a data problem rather
    than a malformed request."""
    assert _probe(db, venue_slug="").status_code == 422


def test_naming_the_venue_still_works(db, hamilton):
    """The other half -- a refusal that refused everything would pass the
    two above while breaking the endpoint."""
    resp = _probe(db, venue_slug=hamilton.slug)

    assert resp.status_code == 200
    assert len(resp.json()["spaces"]) >= 1


def test_an_unknown_venue_is_a_404_not_an_empty_list(db, hamilton):
    """An empty list reads as "no rooms available", which is a booking
    answer. A typo is not a booking answer."""
    resp = _probe(db, venue_slug="not-a-venue")

    assert resp.status_code == 404
    assert "not-a-venue" in resp.text


def test_the_answer_says_which_venue_it_is_about(db, hamilton):
    """A list of rooms and minimum spends that does not name its building
    can be read as being about the other one -- the mislabelled-page failure
    this project has hit four times."""
    body = _probe(db, venue_slug=hamilton.slug).json()

    assert body["venue"] == (hamilton.trading_name or hamilton.name)


def test_no_public_availability_route_carries_a_default_venue():
    """The structural half, by AST, so a default cannot creep back in under
    a different name. Checked against the signature rather than the source
    text, because a comment mentioning the old default would fool a grep --
    which is exactly how an earlier sweep in this project passed."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/api/availability.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args.args
        defaults = dict(zip([a.arg for a in args[len(args) - len(node.args.defaults):]],
                            node.args.defaults))
        for name, default in defaults.items():
            if "venue" not in name:
                continue
            # BOTH shapes. A bare string is one; the idiomatic way to
            # write this default in FastAPI is Query("hamilton", ...),
            # which is a Call -- so a sweep that only recognised Constant
            # reported clean over the very thing it exists to catch.
            if isinstance(default, ast.Call) and default.args:
                default = default.args[0]
            if isinstance(default, ast.Constant) and default.value is Ellipsis:
                continue  # Query(...) is REQUIRED, which is the whole point
            if isinstance(default, ast.Constant) and default.value is not None:
                offenders.append(f"{node.name}({name}={default.value!r})")

    assert not offenders, f"a venue parameter carries a literal default: {offenders}"
