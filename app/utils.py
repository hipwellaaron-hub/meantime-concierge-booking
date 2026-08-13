import re

from email_validator import EmailNotValidError, validate_email


def is_valid_email(email: str | None) -> bool:
    """Syntax-only check (check_deliverability=False) -- this guards a
    staff action against a missing/malformed address already sitting in
    the database (bad import data, a hand-edited record), not a live
    intake form. A live form (see app.schemas.enquiry.EnquiryCreate) uses
    the same email-validator library via Pydantic's EmailStr, so both
    paths agree on what counts as valid."""
    if not email:
        return False
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError:
        return False
    return True


def truncate(value: str, limit: int) -> str:
    """Defensively cap a string before it reaches a fixed-width DB column.
    Validating each individual input field isn't enough on its own --
    values built by combining multiple already-validated fields (e.g. an
    audit-log actor string prefixed onto a user's email) can still exceed
    a column's width even when every input was within its own bound.
    """
    return value if len(value) <= limit else value[:limit]


# Document/Invoice access_token values are always secrets.token_urlsafe(32)
# output: base64url charset only, 43 chars. Anything else (a null byte, a
# path-traversal attempt, an absurdly long garbage string) can never be a
# real token, so it's rejected before ever reaching the database --
# Postgres itself raises a hard, uncatchable-as-a-404 DataError on a NUL
# byte in a text parameter, which used to surface as an unhandled 500.
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def looks_like_a_token(value: str) -> bool:
    return bool(_TOKEN_PATTERN.match(value))
