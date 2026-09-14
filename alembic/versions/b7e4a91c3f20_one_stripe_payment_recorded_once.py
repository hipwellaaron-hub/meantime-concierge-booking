"""one stripe payment is recorded once

The webhook's only defence against recording the same money twice is a
SELECT:

    already_recorded = db.execute(
        select(Payment.id).where(Payment.reference == payment_intent_id)
    ).first()

Nothing in the database enforces it. The check runs before the invoice row
lock is taken and is never re-checked after, so two deliveries of the same
Stripe event can both pass it and both insert. A unique index closes that
window; the SELECT stays, because a clean early return is better than an
IntegrityError for the ordinary redelivery case.

WHY THE INDEX IS PARTIAL, and this is the whole design. `reference` is a
free-text column shared by three very different writers:

  * the Stripe webhook, which stores a PaymentIntent id ("pi_...") and is
    the only one with dedup semantics;
  * the iVvy migration, which stores prose embedding the booking code;
  * staff recording a payment by hand, who type whatever they like -- and
    app/services/legacy_documents.py writes the CONSTANT "Legacy deposit
    PDF uploaded" when a legacy deposit has no source reference, so a
    blanket unique index would refuse the second such booking outright.

So the constraint covers exactly the rows that carry dedup meaning: card
payments whose reference is a Stripe PaymentIntent. Staff can still write
"bank transfer, Tuesday" on two payments, because that is a note and not
an identifier.

THIS MIGRATION REFUSES IF THE DUPLICATE ALREADY EXISTS, rather than
failing on a raw index violation. A duplicated PaymentIntent means real
money was recorded twice against an invoice, and the first thing that
should happen is a person reading which ones -- not a constraint quietly
making the evidence harder to find. The message names them.

Revision ID: b7e4a91c3f20
Revises: e8a1c6f4b209
Create Date: 2026-09-14 12:05:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4a91c3f20"
down_revision: Union[str, Sequence[str], None] = "e8a1c6f4b209"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_payments_stripe_payment_intent"

# The rows the index covers. Kept in one place so the preflight and the
# index cannot drift into checking different sets -- a preflight that
# passes over a narrower set than the index covers is worse than none.
PREDICATE = "method = 'card' AND reference LIKE 'pi\\_%'"


def upgrade() -> None:
    conn = op.get_bind()

    duplicates = conn.execute(
        sa.text(
            f"SELECT reference, count(*) AS n FROM payments "
            f"WHERE {PREDICATE} GROUP BY reference HAVING count(*) > 1 ORDER BY n DESC"
        )
    ).all()
    if duplicates:
        listed = ", ".join(f"{ref} x{n}" for ref, n in duplicates[:10])
        more = "" if len(duplicates) <= 10 else f" (and {len(duplicates) - 10} more)"
        raise RuntimeError(
            f"{len(duplicates)} Stripe PaymentIntent(s) are recorded against more than one payment "
            f"row: {listed}{more}. Each one is the same money counted twice against an invoice, so "
            "the balances derived from it are wrong. Decide what to do with those rows -- refund, "
            "merge or delete -- before this index makes the duplicate impossible to create and "
            "harder to see. Refusing rather than hiding it."
        )

    op.execute(
        f"CREATE UNIQUE INDEX {INDEX_NAME} ON payments (reference) WHERE {PREDICATE}"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
