"""Integration point for the Claude-powered draft-and-approve email
pipeline described in the build brief. That pipeline does not exist yet
(confirmed at the start of this build) -- this is a marked seam for where
it plugs in once built, not a working implementation. It deliberately
does nothing today rather than fake a notification that was never sent.
"""

from app.models import Booking


def notify_new_enquiry(booking: Booking) -> None:
    """Called once a new enquiry is captured. Once the draft pipeline
    exists, this is where it gets triggered to read the booking's
    structured fields and produce a draft reply for human approval --
    per the cross-cutting rule, it should read structured state, not
    re-derive it from raw email threads."""
    return None
