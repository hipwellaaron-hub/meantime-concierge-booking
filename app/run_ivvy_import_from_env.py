"""One-off production import runner. Reads the real iVvy export CSVs from
base64-encoded env vars (IVVY_CSV_1, IVVY_CSV_2) rather than from files in
the repo, so real customer PII (names, emails, phone numbers) never
touches git history. Writes them to a temp dir, then delegates to the
same import_ivvy_csv used and tested locally.

Not wired into any router. Intended to be run once via a temporary
preDeployCommand override, then removed -- and the IVVY_CSV_* variables
deleted from Railway right after.
"""

import base64
import os
import tempfile

from app.database import SessionLocal
from app.models import Venue
from app.services.ivvy_import import import_ivvy_csv

ENV_VARS = ["IVVY_CSV_1", "IVVY_CSV_2"]


def main() -> None:
    db = SessionLocal()
    try:
        venue = db.query(Venue).filter_by(slug="hamilton").one()
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, var_name in enumerate(ENV_VARS, start=1):
                encoded = os.environ.get(var_name)
                if not encoded:
                    print(f"{var_name}: not set, skipping")
                    continue
                path = os.path.join(tmpdir, f"import_{i}.csv")
                with open(path, "wb") as f:
                    f.write(base64.b64decode(encoded))
                result = import_ivvy_csv(db, path, venue=venue)
                print(f"{var_name}: created={result.created} skipped_existing={result.skipped_existing} errors={len(result.errors)}")
                for err in result.errors:
                    print(f"  row {err.row_number} ({err.code}): {err.reason}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
