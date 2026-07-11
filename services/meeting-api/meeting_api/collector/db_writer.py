import logging
import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Set

import redis  # For redis.exceptions
import redis.asyncio as aioredis
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import async_session_local
from ..models import Transcription, Meeting
from .config import BACKGROUND_TASK_INTERVAL, IMMUTABILITY_THRESHOLD

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure factory — no DB side effects
# ---------------------------------------------------------------------------

def create_transcription_object(
    meeting_id: int,
    start: float,
    end: float,
    text: str,
    language: Optional[str],
    session_uid: Optional[str],
    mapped_speaker_name: Optional[str],
    segment_id: Optional[str] = None,
    completed: bool = True,
) -> Transcription:
    """Map a validated domain segment to a Transcription ORM object.

    This function is pure — it creates the object without touching the database
    or changing any external state.
    """
    return Transcription(
        meeting_id=meeting_id,
        start_time=start,
        end_time=end,
        text=text,
        speaker=mapped_speaker_name,
        language=language,
        session_uid=session_uid,
        segment_id=segment_id,
        created_at=datetime.utcnow(),
        status="final" if completed else "draft",
    )


# ---------------------------------------------------------------------------
# Upsert with draft-to-final guard
# ---------------------------------------------------------------------------

async def upsert_transcription(db: AsyncSession, t: Transcription) -> None:
    """INSERT ... ON CONFLICT ... DO UPDATE for a single segment.

    Uniqueness key: (meeting_id, session_uid, segment_id).
    Only permits draft → final refinement (longer or equal text).
    A final segment cannot regress to draft or be replaced by a shorter one.
    """
    if not t.segment_id:
        db.add(t)
        return

    await db.execute(
        sql_text("""
            INSERT INTO transcriptions (
                meeting_id, start_time, end_time, text, speaker,
                language, session_uid, segment_id, created_at, status
            )
            VALUES (:mid, :start, :end, :text, :speaker, :lang,
                    :uid, :segid, :created, :st)
            ON CONFLICT (meeting_id, session_uid, segment_id)
                WHERE segment_id IS NOT NULL
            DO UPDATE SET
                text     = EXCLUDED.text,
                speaker  = EXCLUDED.speaker,
                end_time = EXCLUDED.end_time,
                status   = CASE
                    WHEN transcriptions.status = 'final' THEN 'final'
                    WHEN EXCLUDED.status = 'final' THEN 'final'
                    ELSE 'draft'
                END,
                created_at = CASE
                    WHEN transcriptions.status = 'draft' AND EXCLUDED.status = 'final'
                        THEN EXCLUDED.created_at
                    ELSE transcriptions.created_at
                END
            WHERE
                (
                    transcriptions.status = 'draft'
                    AND (
                        EXCLUDED.status = 'final'
                        OR length(EXCLUDED.text) >= length(transcriptions.text)
                    )
                )
                OR (
                    transcriptions.status = 'final'
                    AND EXCLUDED.status = 'final'
                    AND length(EXCLUDED.text) >= length(transcriptions.text)
                )
        """),
        {
            "mid": t.meeting_id, "start": t.start_time, "end": t.end_time,
            "text": t.text, "speaker": t.speaker, "lang": t.language,
            "uid": t.session_uid, "segid": t.segment_id,
            "created": t.created_at, "st": t.status,
        },
    )


# ---------------------------------------------------------------------------
# Background processor — Redis to Postgres
# ---------------------------------------------------------------------------

async def process_redis_to_postgres(
    redis_c: aioredis.Redis, local_transcription_filter=None,
):
    """Background task: move immutable segments from Redis Hash to Postgres.

    - Commits one bundle transactionally.
    - Acknowledges Redis only after successful commit.
    - On database failure, rolls back and leaves Redis messages pending for retry.
    """
    logger.info("Background Redis-to-PostgreSQL processor started")

    while True:
        try:
            await asyncio.sleep(BACKGROUND_TASK_INTERVAL)

            meeting_ids_raw = await redis_c.smembers("active_meetings")
            if not meeting_ids_raw:
                continue

            batch_to_store = []
            segments_to_delete: Dict[int, Set[str]] = {}

            for meeting_id_str in meeting_ids_raw:
                try:
                    meeting_id = int(meeting_id_str)
                    hash_key = f"meeting:{meeting_id}:segments"
                    redis_segments = await redis_c.hgetall(hash_key)

                    if not redis_segments:
                        await redis_c.srem("active_meetings", meeting_id_str)
                        continue

                    immutability_time = (
                        datetime.now(timezone.utc)
                        - timedelta(seconds=IMMUTABILITY_THRESHOLD)
                    )

                    for seg_key, segment_json in redis_segments.items():
                        try:
                            segment_data = json.loads(segment_json)

                            if "updated_at" not in segment_data:
                                continue

                            updated_at_str = segment_data["updated_at"]
                            if updated_at_str.endswith("Z"):
                                updated_at_str = updated_at_str[:-1] + "+00:00"
                            segment_updated_at = datetime.fromisoformat(updated_at_str)
                            if segment_updated_at.tzinfo is None:
                                segment_updated_at = segment_updated_at.replace(
                                    tzinfo=timezone.utc
                                )

                            if segment_updated_at < immutability_time:
                                start = float(segment_data.get("start_time", 0))
                                end = float(segment_data.get("end_time", 0))
                                if end < start:
                                    start, end = end, start

                                segment_text = segment_data.get("text", "")
                                if not segment_text.strip():
                                    segments_to_delete.setdefault(
                                        meeting_id, set()
                                    ).add(seg_key)
                                    continue

                                batch_to_store.append(
                                    create_transcription_object(
                                        meeting_id=meeting_id,
                                        start=start,
                                        end=end,
                                        text=segment_text,
                                        language=segment_data.get("language"),
                                        session_uid=segment_data.get("session_uid"),
                                        mapped_speaker_name=segment_data.get(
                                            "speaker"
                                        ),
                                        segment_id=segment_data.get("segment_id"),
                                        completed=bool(segment_data.get("completed", True)),
                                    )
                                )
                                segments_to_delete.setdefault(
                                    meeting_id, set()
                                ).add(seg_key)
                        except (
                            json.JSONDecodeError,
                            KeyError,
                            ValueError,
                            TypeError,
                        ) as e:
                            logger.error(
                                "Error processing segment %s for "
                                "meeting %s: %s",
                                seg_key,
                                meeting_id,
                                e,
                            )
                            segments_to_delete.setdefault(
                                meeting_id, set()
                            ).add(seg_key)
                except Exception as e:
                    logger.error(
                        "Error processing meeting %s: %s",
                        meeting_id_str,
                        e,
                        exc_info=True,
                    )

            # Commit one bundle transactionally; ack Redis only after commit
            if batch_to_store:
                async with async_session_local() as db:
                    try:
                        for t in batch_to_store:
                            await upsert_transcription(db, t)
                        await db.commit()
                        logger.info(
                            "Stored %d segments to PostgreSQL", len(batch_to_store)
                        )

                        # Acknowledge Redis only after successful commit
                        for meeting_id, seg_keys in segments_to_delete.items():
                            if seg_keys:
                                hash_key = f"meeting:{meeting_id}:segments"
                                await redis_c.hdel(hash_key, *seg_keys)
                    except Exception as e:
                        logger.error(
                            "Error committing to PostgreSQL: %s",
                            e,
                            exc_info=True,
                        )
                        await db.rollback()
                        # Redis messages remain — will be retried next interval

        except asyncio.CancelledError:
            logger.info("Redis-to-PostgreSQL processor task cancelled")
            break
        except redis.exceptions.ConnectionError as e:
            logger.error("Redis connection error: %s. Retrying...", e, exc_info=True)
            await asyncio.sleep(5)
        except Exception as e:
            logger.error("Unhandled error in Redis-to-PG: %s", e, exc_info=True)
            await asyncio.sleep(BACKGROUND_TASK_INTERVAL)
