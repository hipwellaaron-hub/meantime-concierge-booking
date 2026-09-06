"""Event Order proposals: propose-and-approve for the ten free-text fields

Two tables. `beo_proposals` is one AI ask against one booking, with the
source it was derived from and the house-rule verdict; `beo_proposal_fields`
is one row per proposed field, carrying what was proposed, what the Event
Order held before, and what was actually written on approval.

Nothing here touches the documents table: a proposal is applied by the
existing hand-edit path (documents.update_content_fields), which already
refuses anything that is not a draft.

Two constraints are load-bearing rather than decorative:
- the partial unique index gives "one pending proposal per booking" at the
  database level, so two concurrent proposals cannot both survive with one
  of them invisible forever;
- the CHECK constraints keep the status columns to the values the code
  compares against, since a row written by hand or by a future backfill
  with a different spelling would be permanently unreviewable.

Revision ID: a3f6e1c7d094
Revises: b4c1e8f27a93
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3f6e1c7d094"
down_revision = "b4c1e8f27a93"
branch_labels = None
depends_on = None

PROPOSAL_STATUSES = ("pending", "rules_blocked", "resolved", "superseded")
FIELD_STATES = ("pending", "approved", "rejected", "superseded", "blocked")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "beo_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "booking_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(500), nullable=False),
        sa.Column("trigger", sa.String(30), nullable=True),
        sa.Column("model", sa.String(80), nullable=True),
        sa.Column("rule_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rule_note", sa.Text(), nullable=True),
        sa.Column("warning_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("warning_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in_list("status", PROPOSAL_STATUSES), name="ck_beo_proposal_status"),
    )
    op.create_index("ix_beo_proposals_booking_id", "beo_proposals", ["booking_id"])
    op.create_index("ix_beo_proposals_booking_created", "beo_proposals", ["booking_id", "created_at"])
    op.create_index(
        "uq_beo_proposal_one_pending_per_booking",
        "beo_proposals",
        ["booking_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "beo_proposal_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "proposal_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("beo_proposals.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("field", sa.String(50), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("proposed_value", sa.Text(), nullable=False),
        sa.Column("previous_value", sa.Text(), nullable=True),
        sa.Column("applied_value", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.UniqueConstraint("proposal_id", "field", name="uq_beo_proposal_field"),
        sa.CheckConstraint(_in_list("state", FIELD_STATES), name="ck_beo_proposal_field_state"),
    )
    op.create_index("ix_beo_proposal_fields_proposal_id", "beo_proposal_fields", ["proposal_id"])


def downgrade() -> None:
    op.drop_index("ix_beo_proposal_fields_proposal_id", table_name="beo_proposal_fields")
    op.drop_table("beo_proposal_fields")
    op.drop_index("uq_beo_proposal_one_pending_per_booking", table_name="beo_proposals")
    op.drop_index("ix_beo_proposals_booking_created", table_name="beo_proposals")
    op.drop_index("ix_beo_proposals_booking_id", table_name="beo_proposals")
    op.drop_table("beo_proposals")
