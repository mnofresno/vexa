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

    # 3. Replace the old meeting+segment unique index with the session-aware
    #    identity. PostgreSQL treats NULLs as distinct, so session_uid must be
    #    present on new segment messages for this to enforce idempotency.
    op.execute("DROP INDEX IF EXISTS ix_transcription_meeting_segment")
    op.create_index(
        "ix_transcription_meeting_segment",
        "transcriptions",
        ["meeting_id", "segment_id"],
        unique=False,
    )
    op.create_index(
        "uq_transcription_meeting_session_segment",
        "transcriptions",
        ["meeting_id", "session_uid", "segment_id"],
        unique=True,
        postgresql_where=sa.text("segment_id IS NOT NULL"),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transcription_meeting_start "
        "ON transcriptions (meeting_id, start_time)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transcriptions_text_tsvector "
        "ON transcriptions USING gin (to_tsvector('spanish', text))"
    )


def downgrade() -> None:
    op.drop_index("uq_transcription_meeting_session_segment", table_name="transcriptions")
    op.execute("DROP INDEX IF EXISTS ix_transcriptions_text_tsvector")
    op.drop_index("ix_transcription_meeting_segment", table_name="transcriptions")
    op.create_index(
        "ix_transcription_meeting_segment",
        "transcriptions",
        ["meeting_id", "segment_id"],
        unique=True,
        postgresql_where=sa.text("segment_id IS NOT NULL"),
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
