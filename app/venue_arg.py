"""Resolving the venue a command-line script operates on.

Every one of the import/reconcile scripts used to open with

    venue = db.query(Venue).filter_by(slug="hamilton").one()

which is a script that writes bookings into whichever venue somebody
hardcoded, whatever CSV you hand it. With one venue that is invisible; with
two it is an import of one company's functions into the other's books, and
`bookings.venue_id` is immutable by database trigger, so the remedy is
delete-and-recreate rather than an UPDATE.

REQUIRED, NOT DEFAULTED. A script that guesses a venue is the same class of
mistake as a payment link minted with a guessed key: it succeeds, silently,
into the wrong company. Making the argument required costs whoever runs it
one more word and removes the whole failure.
"""

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Venue


class VenueArgumentError(SystemExit):
    """Exits non-zero with a usable message rather than a traceback --
    these are run by a human at a prompt, not by another program."""


def resolve_venue(db: Session, slug: str | None, *, usage: str) -> Venue:
    """The venue named by --venue, or a refusal listing what is available.

    Never falls back to a default. The error names every slug in the
    database, because the most likely reason for getting here is a typo and
    the second most likely is not knowing the spelling."""
    known = [v.slug for v in db.scalars(select(Venue).order_by(Venue.slug)).all()]

    if not slug:
        raise VenueArgumentError(
            f"--venue is required.\n{usage}\n"
            f"Known venues: {', '.join(known) if known else '(none -- the database has no venues)'}"
        )

    venue = db.scalars(select(Venue).where(Venue.slug == slug)).one_or_none()
    if venue is None:
        raise VenueArgumentError(
            f"No venue with slug {slug!r}.\n"
            f"Known venues: {', '.join(known) if known else '(none -- the database has no venues)'}"
        )
    return venue


def take_venue_arg(argv: list[str]) -> tuple[str | None, list[str]]:
    """Pull `--venue X` (or `--venue=X`) out of argv, returning
    (slug, remaining args).

    Hand-rolled rather than argparse because these scripts take a variable
    number of positional paths and one of them takes a --report flag; adding
    argparse to four scripts is a larger change than the venue argument
    itself, and this is the whole grammar.
    """
    slug: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--venue":
            if i + 1 >= len(argv):
                raise VenueArgumentError("--venue needs a slug after it, e.g. --venue hamilton")
            slug = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--venue="):
            slug = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return slug, rest
