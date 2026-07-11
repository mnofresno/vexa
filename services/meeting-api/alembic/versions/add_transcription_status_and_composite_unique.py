"""add transcription status column and composite unique constraint

Revision ID: pr3_transcription_status_composite_uq
Revises:
Create Date: 2026-07-11

PR3 — Guarantee Redis publication and Postgres persistence:
- Add `status` column (draft / final) with default 'draft'.
- Add composite unique constraint on (meeting_id, session_uid, segment_id).
- Backfill legacy NULL segment_ids with deterministic UUIDs so the
  constraint can be created without violating existing rows.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pr3_transcription_status_composite_uq"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the status column
    op.add_column(
        "transcriptions",
        sa.Column("status", sa.String(10), nullable=False, server_default="draft"),
    )

    # 2. Backfill legacy NULL segment_ids with deterministic UUIDs
    #    built from meeting_id, start_time, end_time, and text hash.
    #    This lets us create the unique constraint without violating rows
    #    that were inserted before segment_id was used.
    op.execute(
        """
        UPDATE transcriptions
        SET segment_id = md5(
            meeting_id::text || '-' ||
            start_time::text || '-' ||
            end_time::text || '-' ||
            substring(text from 1 for 100)
        )
        WHERE segment_id IS NULL
        AND session_uid IS NOT NULL
        """
    )

    # 3. Create the composite unique constraint (partial — only when
    #    segment_id IS NOT NULL, so legacy rows without session_uid are safe).
    op.create_unique_constraint(
        "uq_transcription_meeting_session_segment",
        "transcriptions",
        ["meeting_id", "session_uid", "segment_id"],
        postgresql_where=postgresql.text("segment_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Reverse the unique constraint
    op.drop_constraint(
        "uq_transcription_meeting_session_segment",
        "transcriptions",
        type_="unique",
    )

    # Reverse the status column
    op.drop_column("transcriptions", "status")

    # Note: the backfilled segment_ids are left in place — removing them
    # would risk creating actual duplicates.  Callers that need pristine
    # legacy state should truncate and reimport.
    op.execute(
        "COMMENT ON COLUMN transcriptions.segment_id IS "
        "'backfilled by pr3 — do not nullify after upgrade'"
    )
