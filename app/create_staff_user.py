"""One-off script: provisions (or updates) a staff login. There is no
public signup route anywhere in this app -- this script is the only way
a staff_users row gets created, run by a human, same pattern as every
other one-off script in this codebase.

Reads STAFF_EMAIL / STAFF_NAME / STAFF_PASSWORD from the environment
rather than CLI args, so a real password is never a shell-history/process-
list-visible argument -- same reasoning as the base64-env-var approach
used for the real iVvy customer CSV import. Idempotent: re-running against
the same email resets that account's name/password rather than erroring.

    STAFF_EMAIL=aaron@meantime.com.au STAFF_NAME="Aaron Hipwell" STAFF_PASSWORD=... \
        python -m app.create_staff_user
"""

import os

from app.database import SessionLocal
from app.services.staff_auth import create_or_update_staff_user


def main() -> None:
    email = os.environ.get("STAFF_EMAIL", "").strip()
    name = os.environ.get("STAFF_NAME", "").strip()
    password = os.environ.get("STAFF_PASSWORD", "")

    if not email or not name or not password:
        raise SystemExit("STAFF_EMAIL, STAFF_NAME, and STAFF_PASSWORD must all be set")
    if len(password) < 12:
        raise SystemExit("STAFF_PASSWORD must be at least 12 characters")

    db = SessionLocal()
    try:
        staff = create_or_update_staff_user(db, email=email, name=name, password=password)
        print(f"Staff user ready: {staff.email} ({staff.name})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
