import re
import secrets

from email_validator import EmailNotValidError, validate_email

# Bytes of randomness behind every client-facing access token (document,
# invoice, wizard link). 16 bytes is 128 bits -- more entropy than a
# random UUID, which is the usual standard for an unguessable link, and
# far beyond brute-forcing: an attacker checking a billion tokens a
# second would still need longer than the age of the universe.
#
# Chosen over the original 32 bytes purely for length. These tokens go in
# links pasted into client emails, and 32 bytes produced a 43-character
# token that made an already-long URL look like spam. 16 bytes halves
# that to 22 characters with no practical loss of security. Not reduced
# further: an agreement link is enough to *sign* a contract, so this
# stays comfortably above the point where guessing is even discussable.
ACCESS_TOKEN_BYTES = 16


def generate_access_token() -> str:
    """The shared generator for every client-facing link token. Tokens
    already issued keep working: they're looked up by exact match, and
    the column is wide enough for both lengths, so a client holding an
    older 43-character link is unaffected."""
    return secrets.token_urlsafe(ACCESS_TOKEN_BYTES)


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


# Document/Invoice access_token values are secrets.token_urlsafe output:
# base64url charset only. Deliberately not pinned to one exact length --
# tokens issued before ACCESS_TOKEN_BYTES was reduced are 43 characters
# and must keep resolving, while new ones are 22. Anything outside the
# charset (a null byte, a path-traversal attempt, an absurdly long
# garbage string) can never be a real token, so it's rejected before ever
# reaching the database -- Postgres itself raises a hard,
# uncatchable-as-a-404 DataError on a NUL byte in a text parameter, which
# used to surface as an unhandled 500.
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def looks_like_a_token(value: str) -> bool:
    return bool(_TOKEN_PATTERN.match(value))


def format_person_name(name: str | None) -> str | None:
    """Display casing for a person's name on client-facing documents.

    Contact names are stored exactly as typed into the enquiry form, and
    clients routinely type all-lowercase ("ruby hipwell") -- which then
    headlines their own Event Order. Recased ONLY when the input is
    entirely lowercase or entirely uppercase (clearly un-cased typing);
    anything mixed-case is deliberate ("McDonald", "van der Berg") and is
    never touched, because a wrong "fix" to a real name is worse than
    none. Capitalizes after spaces, hyphens and apostrophes so
    "mary-jane o'brien" comes out "Mary-Jane O'Brien".
    """
    if not name:
        return name
    stripped = name.strip()
    if not stripped or not (stripped.islower() or stripped.isupper()):
        return stripped
    result = []
    capitalize_next = True
    for ch in stripped.lower():
        result.append(ch.upper() if capitalize_next and ch.isalpha() else ch)
        capitalize_next = ch in (" ", "-", "'")
    return "".join(result)


def format_date_dmy(value) -> str:
    """Australian day-first display format, DD-MM-YYYY.

    Accepts a date/datetime, an ISO "YYYY-MM-DD" string (how dates sit
    frozen inside stored document content), or anything else -- which
    passes through untouched, so placeholders like "Date TBD" and
    "[REVIEW] ..." markers survive being piped through the template
    filter. Display only: date <input>s, URLs and JSON payloads keep ISO,
    which browsers and parsers require.
    """
    import datetime as _dt

    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        value = value.date()
    if isinstance(value, _dt.date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value.strip()).strftime("%d-%m-%Y")
        except ValueError:
            return value
    return str(value)
