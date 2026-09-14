"""Clearing a client's IP and user agent does not depend on tracking being on.

Aaron, 2026-09-14: "make sure it runs independently of the dispatch gate,
since the whole point is that turning dispatch off shouldn't strand the
data."

The only code that ever deletes a stored client IP address and user agent
is conversions.retire_tracking_context, and it was reachable only from
conversions.run_sweep -- which sits behind TRACKING_SERVER_DISPATCH_ENABLED.
So turning server-side ad dispatch OFF also turned off the deletion, and
the personal data it exists to remove would sit in the database
indefinitely. The switch meant to reduce what is held did the opposite.

AND IT COULD NOT REACH OLD ROWS. The query carried an undocumented LOWER
bound -- `since = cutoff - 60 days` -- so anything older than roughly 74
days was permanently skipped: the rows most overdue were the only ones it
would not clear. Retention that cannot reach old data is not retention.
"""
import datetime as dt

from app.services import conversions
from app.services.booking import create_booking

OLD_CONTEXT = {"client_ip": "203.0.113.7", "user_agent": "Mozilla/5.0", "_fbp": "fb.1.keepme"}


def _booking_with_context(db, loft, contact, *, days_old, name="Tracked"):
    b = create_booking(
        db, space_id=loft.id, contact_id=contact.id,
        event_date=dt.date.today() + dt.timedelta(days=60), start_time=dt.time(18, 0),
        end_time=dt.time(23, 30), event_name=name, event_type="birthday",
        adult_count=60, child_count=0, notes=None, actor="test",
    )
    db.flush()
    b.tracking_context = dict(OLD_CONTEXT)
    b.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_old)
    db.flush()
    return b


def _now():
    return dt.datetime.now(dt.timezone.utc)


# --- the reach --------------------------------------------------------------


def test_a_very_old_booking_is_cleared(db, hamilton, loft, contact):
    """THE one the lower bound made unreachable. A year-old row is the most
    overdue thing in the table and was the one guaranteed to be skipped."""
    booking = _booking_with_context(db, loft, contact, days_old=365, name="ZZRETIRE Ancient")

    cleared = conversions.retire_tracking_context(db, now=_now())
    db.flush()

    assert cleared >= 1
    assert "client_ip" not in booking.tracking_context
    assert "user_agent" not in booking.tracking_context


def test_the_cookie_ids_are_deliberately_kept(db, hamilton, loft, contact):
    """They carry no more than the analytics platforms already hold, and
    they are what a later reconciliation would need."""
    booking = _booking_with_context(db, loft, contact, days_old=365, name="ZZRETIRE Cookies")

    conversions.retire_tracking_context(db, now=_now())
    db.flush()

    assert booking.tracking_context.get("_fbp") == "fb.1.keepme"


def test_a_recent_booking_is_left_alone(db, hamilton, loft, contact):
    """Still inside its sending window -- clearing it would break the thing
    the data is held for."""
    booking = _booking_with_context(db, loft, contact, days_old=0, name="ZZRETIRE Fresh")

    conversions.retire_tracking_context(db, now=_now())
    db.flush()

    assert booking.tracking_context.get("client_ip") == "203.0.113.7"


# --- independent of the dispatch gate ---------------------------------------


def test_the_job_does_not_read_the_dispatch_flag():
    """Structural, and the point of the whole item: this entrypoint must
    not be gated on the switch whose being OFF is precisely when the
    deletion matters most."""
    import ast
    import pathlib

    source = pathlib.Path("app/retire_tracking_context.py").read_text(encoding="utf-8")

    # PARSED, not grepped. The module's own docstring names the flag it is
    # deliberately NOT gated on -- a raw substring search matches that
    # explanation and fails on a correct file, which is a check that
    # punishes writing down the reason.
    tree = ast.parse(source)
    names = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }

    for gate in ("TRACKING_SERVER_DISPATCH_ENABLED", "dispatch_enabled", "run_sweep",
                 "tracking_server_dispatch_enabled"):
        assert gate not in names, f"the deletion job is gated on {gate}"
    # And the positive control: it really does call the thing that deletes,
    # so an empty parse cannot pass this test.
    assert "retire_tracking_context" in names
    assert "count_tracking_context_due" in names


def test_the_dry_run_counts_without_clearing(db, hamilton, loft, contact):
    """A retention job you cannot rehearse is one nobody runs."""
    booking = _booking_with_context(db, loft, contact, days_old=365, name="ZZRETIRE DryRun")

    due = conversions.count_tracking_context_due(db, now=_now())

    assert due >= 1
    assert booking.tracking_context.get("client_ip") == "203.0.113.7", "the dry run cleared data"


def test_counting_and_clearing_agree(db, hamilton, loft, contact):
    """The count is what the dry run prints and the clear is what the real
    run does; if they disagreed, the rehearsal would be a lie."""
    _booking_with_context(db, loft, contact, days_old=365, name="ZZRETIRE AgreeA")
    _booking_with_context(db, loft, contact, days_old=400, name="ZZRETIRE AgreeB")

    due = conversions.count_tracking_context_due(db, now=_now())
    cleared = conversions.retire_tracking_context(db, now=_now())

    assert cleared == due
