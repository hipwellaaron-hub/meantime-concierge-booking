"""Can this venue actually serve a client today?

Every fact here was already knowable and none of it was being asked at
runtime. `python -m app.seed` reported the unfilled columns once, at
deploy, into a log line read by whoever ran the deploy on the day they ran
it -- and a second venue's row is typed in by hand, days after that deploy.

THE 500 THIS EXISTS FOR, proven by running it on 2026-09-14: a venue row
with all fourteen client-facing columns filled and NO spaces serves
`GET /enquire/{slug}` as a normal 200 page, and returns 500 on the POST.
`enquiry_classification.create_enquiry_booking` files every public enquiry
against the venue's "Unassigned (pending triage)" space and reaches it
through `ivvy_import.get_unassigned_space_id`, which is a `scalar_one()`.
No space, no row, NoResultFound. The client fills in the form, gets
Internal Server Error, and there is no booking, no notification and
nothing in the digest -- a lead lost silently on the first day of a venue.

So the gaps are asked at runtime, in the two places that get looked at:
`/healthz` folds them to one boolean (detail to the log -- the endpoint is
public and the missing details of a named company are not a fact a
monitoring URL hands out), and the 20:30 digest names them, because Aaron's
rule of 2026-09-14 is that a check that fires without reaching the digest
is not finished.

THE COLUMN LIST IS SEED'S, not a second copy. A second list of "which
columns matter" is exactly how a deploy log comes to say ready while an
email says not.
"""

import dataclasses

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Space, Venue
from app.seed import HARD_BLOCK_COLUMNS, UNASSIGNED_SPACE_NAME, unfilled_columns

# A gap that stops the venue working carries the sentence saying HOW, because
# "reference_prefix" and "unassigned_space" read as equally cosmetic in a list
# and neither one is. A gap with no entry here prints blank on a document,
# which is bad differently.
BLOCKING_CONSEQUENCE = {
    "reference_prefix": (
        "this venue cannot take a booking or issue an invoice at all -- both refuse rather "
        "than stamp another company's letters on a reference"
    ),
    UNASSIGNED_SPACE_NAME: (
        "the public enquiry form renders but 500s on submit, and the lead is lost with no "
        "booking and no notification"
    ),
    "a bookable space": (
        "nothing can be assigned a room, and availability offers nothing"
    ),
}


def partition(gaps) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(blocking, cosmetic). ONE implementation of the split, because the
    digest renders it and Readiness.blocking reports it, and a mutation
    that widened the property alone survived a probe on 2026-09-14 -- the
    digest was partitioning the same list a second time, so nothing
    disagreed."""
    blocking = tuple(g for g in gaps if g in BLOCKING_CONSEQUENCE)
    cosmetic = tuple(g for g in gaps if g not in BLOCKING_CONSEQUENCE)
    return blocking, cosmetic


@dataclasses.dataclass(frozen=True)
class Readiness:
    slug: str
    gaps: tuple[str, ...]

    @property
    def is_ready(self) -> bool:
        return not self.gaps

    @property
    def blocking(self) -> tuple[str, ...]:
        return partition(self.gaps)[0]


def check(db: Session, venue: Venue) -> Readiness:
    gaps: list[str] = list(unfilled_columns(venue))

    spaces = db.scalars(select(Space).where(Space.venue_id == venue.id)).all()
    if not any(s.name == UNASSIGNED_SPACE_NAME for s in spaces):
        gaps.append(UNASSIGNED_SPACE_NAME)
    if not any(s.is_bookable for s in spaces):
        # Separate from the one above, because they are separate failures: a
        # venue can have the triage space and no room to sell, which takes
        # the enquiry fine and can never fulfil it.
        gaps.append("a bookable space")

    return Readiness(slug=venue.slug, gaps=tuple(gaps))


def _assert_blocking_keys_exist() -> None:
    """HARD_BLOCK_COLUMNS and BLOCKING_CONSEQUENCE name the same columns.

    Import-time, because the failure is silent: rename a column in one and
    the digest quietly stops saying why the venue cannot take a booking,
    with every test still green.
    """
    missing = [c for c in HARD_BLOCK_COLUMNS if c not in BLOCKING_CONSEQUENCE]
    if missing:  # pragma: no cover -- the assertion is the point
        raise RuntimeError(
            f"seed.HARD_BLOCK_COLUMNS names {missing}, which venue_readiness has no "
            "consequence sentence for -- the digest would list it as if it printed blank"
        )


_assert_blocking_keys_exist()
