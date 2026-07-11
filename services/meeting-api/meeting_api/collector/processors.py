import logging
import json
import uuid
import os
import hmac
import base64
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, Any, Optional, List, Tuple

import redis # For redis.exceptions
import redis.asyncio as aioredis # For type hinting redis_client
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import async_session_local
from ..models import Meeting, MeetingSession
from ..schemas import Platform
from .config import REDIS_SEGMENT_TTL, REDIS_SPEAKER_EVENT_KEY_PREFIX, REDIS_SPEAKER_EVENT_TTL, REDIS_STREAM_NAME

logger = logging.getLogger(__name__)

# ── PR3: Explicit acknowledgement decisions ─────────────────────────────
class AckDecision(Enum):
    """Return value for stream message processing decisions."""
    ACK = "ACK"              # Message processed successfully; safe to ACK.
    RETRY = "RETRY"          # Transient failure; leave message pending for retry.
    DEAD_LETTER = "DEAD_LETTER"  # Unrecoverable; move to dead-letter stream.


# Supported schema versions for incoming segment envelopes.
# New future versions not in this set go to dead-letter.
SUPPORTED_SCHEMA_VERSIONS = {1}

# Dead-letter stream name for messages that cannot be processed.
REDIS_DEAD_LETTER_STREAM = os.environ.get(
    "REDIS_DEAD_LETTER_STREAM", "transcription_dead_letter"
)


async def _send_to_dead_letter(message_id: str, message_data: Dict[str, Any], reason: str, redis_c: aioredis.Redis) -> None:
    """Publish a message to the dead-letter stream with diagnostic metadata."""
    try:
        envelope = {
            "original_message_id": message_id,
            "reason": reason,
            "original_payload": message_data.get("payload", ""),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        await redis_c.xadd(REDIS_DEAD_LETTER_STREAM, envelope)
        logger.info(f"Dead-lettered message {message_id}: {reason}")
    except redis.exceptions.RedisError as e:
        logger.error(f"Failed to write dead-letter for {message_id}: {e}", exc_info=True)


# ── PR3-3: Single timestamp normalization function ──────────────────────
def _normalize_timestamps(start, end) -> Tuple[Optional[float], Optional[float]]:
    """Normalize raw segment timestamps to float seconds.

    SINGLE place timestamps are interpreted. Callers must not reparse or
    reinterpret the returned values.

    Accepts numeric (int/float/str) and ISO-8601 datetime strings.
    Returns (start, end) as floats; returns (None, None) on invalid input.
    Swaps if inverted.
    """
    def _to_float(val):
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                s = val
                if s.endswith('Z'):
                    s = s[:-1]
                dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
                return dt.timestamp()
        raise TypeError(f"Unsupported timestamp type: {type(val)}")

    try:
        s = _to_float(start)
        e = _to_float(end)
    except (ValueError, TypeError):
        return None, None

    if s is None or e is None:
        return s, e

    if e < s:
        s, e = e, s

    return s, e

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _b64url_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def verify_meeting_token(token: str) -> Optional[dict]:
    try:
        if not token:
            return None
        secret = os.environ.get("ADMIN_TOKEN") or os.environ.get("ADMIN_API_TOKEN")
        if not secret:
            logger.error("ADMIN_TOKEN not set; cannot verify MeetingToken")
            return None
        parts = token.split('.')
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature_b64 = parts
        header_json = _b64url_decode(header_b64)
        payload_json = _b64url_decode(payload_b64)
        header = json.loads(header_json)
        payload = json.loads(payload_json)
        if header.get('alg') != 'HS256' or header.get('typ') != 'JWT':
            return None
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected_sig = hmac.new(secret.encode("utf-8"), signing_input, digestmod='sha256').digest()
        expected_b64 = _b64url_encode(expected_sig)
        if not hmac.compare_digest(expected_b64, signature_b64):
            return None
        # Basic claims checks
        now = int(datetime.now(timezone.utc).timestamp())
        if 'exp' in payload and int(payload['exp']) < now:
            return None
        if payload.get('aud') != 'transcription-collector' or payload.get('iss') != 'meeting-api':
            return None
        if payload.get('scope') != 'transcribe:write':
            return None
        if 'meeting_id' not in payload:
            return None
        return payload
    except Exception as e:
        logger.warning(f"MeetingToken verification failed: {e}")
        return None

async def process_session_start_event(message_id: str, stream_data: Dict[str, Any], db: AsyncSession, user: None, meeting: Meeting, redis_c: aioredis.Redis) -> AckDecision:
    """Process a session_start event.

    Updates the MeetingSession database record with the accurate start time.
    Caches session start time in Redis for fast absolute timestamp computation.

    PR3: Returns an explicit AckDecision.
    """
    try:
        required_fields = ["uid", "start_timestamp"]
        if not all(field in stream_data for field in required_fields):
            logger.warning(f"Session start message {message_id} missing required fields for session processing. Skipping. Required: {required_fields}")
            return AckDecision.ACK

        # ── PR3-3: Normalize the start timestamp through the single entry point ──
        start_ts = _normalize_timestamps(stream_data['start_timestamp'], None)
        start_time_float = start_ts[0]
        if start_time_float is None:
            logger.warning(f"Invalid start_timestamp in session_start message {message_id}: {stream_data['start_timestamp']}")
            return AckDecision.ACK

        start_timestamp = datetime.fromtimestamp(start_time_float, tz=timezone.utc)

        session_uid = stream_data['uid']
        stmt_session = select(MeetingSession).where(
            MeetingSession.meeting_id == meeting.id,
            MeetingSession.session_uid == session_uid
        )
        result_session = await db.execute(stmt_session)
        meeting_session = result_session.scalars().first()

        if meeting_session:
            meeting_session.session_start_time = start_timestamp
            logger.info(f"Updated start time for existing session {session_uid}, meeting_id {meeting.id} to {start_timestamp}")
        else:
            meeting_session = MeetingSession(
                meeting_id=meeting.id,
                session_uid=session_uid,
                session_start_time=start_timestamp
            )
            db.add(meeting_session)
            logger.info(f"Created new session {session_uid} for meeting_id {meeting.id} with start time {start_timestamp}")

        await db.commit()

        # Cache session start time in Redis for fast lookup during transcription
        try:
            session_start_cache_key = f"meeting_session:{session_uid}:start"
            await redis_c.set(session_start_cache_key, start_timestamp.isoformat(), ex=7200)
            logger.info(f"Cached session start time in Redis: {session_start_cache_key}")
        except Exception as redis_err:
            logger.warning(f"Failed to cache session start time in Redis for session {session_uid}: {redis_err}")

        logger.info(f"Successfully processed session_start event for meeting {meeting.id}, session {session_uid}")
        return AckDecision.ACK

    except Exception as e:
        logger.error(f"Error processing session_start_event for message {message_id}, meeting {meeting.id if meeting else 'Unknown'}: {e}", exc_info=True)
        try:
            await db.rollback()
        except Exception as rb_err:
            logger.error(f"Failed to rollback after error in process_session_start_event: {rb_err}", exc_info=True)
        return AckDecision.RETRY

async def process_stream_message(message_id: str, message_data: Dict[str, Any], redis_c: aioredis.Redis) -> AckDecision:
    """Process a single message payload from the Redis stream.

    PR3: Returns an explicit AckDecision — ACK, RETRY, or DEAD_LETTER.
    """
    payload_json = ""
    try:
        if 'payload' not in message_data:
            logger.warning(f"Message {message_id} missing 'payload' field. Skipping.")
            return AckDecision.ACK

        payload_json = message_data['payload']
        stream_data = json.loads(payload_json)

        # ── PR3-1: Parse schema_version and reject unknown future versions ──
        schema_version = stream_data.get('schema_version')
        if schema_version is None:
            # Legacy messages without schema_version default to v1 (backward compat)
            schema_version = 1
        try:
            schema_version = int(schema_version)
        except (ValueError, TypeError):
            logger.warning(f"Message {message_id} has invalid schema_version '{schema_version}'. Dead-lettering.")
            await _send_to_dead_letter(message_id, message_data, f"invalid schema_version: {schema_version}", redis_c)
            return AckDecision.DEAD_LETTER
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            logger.warning(f"Message {message_id} schema_version={schema_version} not supported. Dead-lettering.")
            await _send_to_dead_letter(message_id, message_data, f"unsupported schema_version: {schema_version}", redis_c)
            return AckDecision.DEAD_LETTER

        message_type = stream_data.get("type", "transcription")

        user = None
        meeting: Optional[Meeting] = None
        internal_meeting_id: Optional[int] = None

        async with async_session_local() as db:
            try:
                # Verify MeetingToken and extract claims.
                # Internal Redis stream messages are trusted — if no token is
                # present, fall back to stream_data fields directly.
                token = stream_data.get('token')
                claims = verify_meeting_token(token) if token else None
                if claims:
                    internal_meeting_id = int(claims.get('meeting_id'))
                    platform_val = claims.get('platform') or stream_data.get('platform')
                    native_meeting_id = claims.get('native_meeting_id') or stream_data.get('meeting_id')
                elif not token:
                    # No token provided — trusted internal message
                    raw_mid = stream_data.get('meeting_id')
                    if not raw_mid:
                        logger.warning(f"Message {message_id} (type: {message_type}) has no token and no meeting_id. Skipping.")
                        return AckDecision.ACK
                    internal_meeting_id = int(raw_mid)
                    platform_val = stream_data.get('platform')
                    native_meeting_id = stream_data.get('native_meeting_id')
                    logger.debug(f"Message {message_id} accepted without token (internal stream)")
                else:
                    logger.warning(f"Message {message_id} (type: {message_type}) failed MeetingToken verification. Skipping.")
                    return AckDecision.ACK

                # ── PR3-2: Validate meeting ownership before processing ──
                meeting = await db.get(Meeting, internal_meeting_id)
                if not meeting:
                    logger.warning(f"Message {message_id} references unknown meeting_id {internal_meeting_id}. Dead-lettering.")
                    await _send_to_dead_letter(message_id, message_data, f"unknown meeting_id: {internal_meeting_id}", redis_c)
                    return AckDecision.DEAD_LETTER

                # ── PR3-2: Validate session ownership (when uid is provided) ──
                session_uid = stream_data.get('session_uid') or stream_data.get('uid')
                if session_uid and message_type in ("transcription", "transcript"):
                    stmt_session = select(MeetingSession).where(
                        MeetingSession.meeting_id == internal_meeting_id,
                        MeetingSession.session_uid == session_uid,
                    )
                    result_session = await db.execute(stmt_session)
                    if not result_session.scalars().first():
                        logger.warning(
                            f"Message {message_id} session_uid '{session_uid}' "
                            f"does not belong to meeting {internal_meeting_id}. Dead-lettering."
                        )
                        await _send_to_dead_letter(
                            message_id,
                            message_data,
                            f"session_uid '{session_uid}' not owned by meeting {internal_meeting_id}",
                            redis_c,
                        )
                        return AckDecision.DEAD_LETTER

                # Process different message types
                if message_type == "session_start":
                    return await process_session_start_event(message_id, stream_data, db, None, meeting, redis_c)
                elif message_type == "transcription":
                    pass # Continue with transcription processing (legacy)
                elif message_type == "transcript":
                    # New format: per-speaker bundle with confirmed + pending
                    return await process_transcript_bundle(message_id, stream_data, internal_meeting_id, redis_c)
                elif message_type == "session_end":  # Handle session_end for cleanup
                    if not session_uid:
                        logger.warning(f"Message {message_id} (type: session_end) missing 'uid'. Skipping cleanup.")
                        return AckDecision.ACK

                    speaker_event_key = f"{REDIS_SPEAKER_EVENT_KEY_PREFIX}:{session_uid}"
                    session_start_cache_key = f"meeting_session:{session_uid}:start"
                    try:
                        deleted_count = await redis_c.delete(speaker_event_key, session_start_cache_key)
                        logger.info(f"Processed session_end for UID '{session_uid}'. Deleted speaker events and session start cache from Redis (count: {deleted_count}).")
                    except redis.exceptions.RedisError as e_redis:
                        logger.error(f"Redis error deleting keys for '{session_uid}' on session_end: {e_redis}")
                        return AckDecision.RETRY
                    return AckDecision.ACK
                else:
                    logger.warning(f"Message {message_id} has unknown type '{message_type}'. Skipping.")
                    return AckDecision.ACK

            except ValueError as ve:
                logger.warning(f"Auth/Lookup or validation failed for message {message_id}: {ve}. Skipping.")
                return AckDecision.ACK
            except Exception as db_err:
                logger.error(f"DB/Lookup error preparing for message {message_id}: {db_err}", exc_info=True)
                await db.rollback()
                return AckDecision.RETRY

            # --- Transcription type processing ---
            required_fields_transcription = ["segments"]
            if not all(field in stream_data for field in required_fields_transcription):
                 logger.warning(f"Transcription message {message_id} payload missing 'segments' field. Skipping. Payload: {payload_json[:200]}...")
                 return AckDecision.ACK

            segment_count = 0
            hash_key = f"meeting:{internal_meeting_id}:segments"
            segments_to_store = {}
            session_uid_from_payload = stream_data.get('session_uid') or stream_data.get('uid')

            if not session_uid_from_payload:
                logger.warning(f"[Msg {message_id}/Meet {internal_meeting_id}] Message missing session_uid/uid for transcription segments. Cannot map speakers. Segments in this message will not have speaker info.")

            for i, segment in enumerate(stream_data.get('segments', [])):
                 if not isinstance(segment, dict) or segment.get('start') is None or segment.get('end') is None:
                     logger.warning(f"[Msg {message_id}/Meet {internal_meeting_id}] Skipping segment {i} missing structure or 'start'/'end': {segment}")
                     continue

                 # ── PR3-4: Preserve upstream segment_id (from 'id' or legacy 'segment_id') ──
                 segment_id = segment.get('id') or segment.get('segment_id')
                 if not segment_id:
                     logger.warning(f"[Msg {message_id}/Meet {internal_meeting_id}] Segment {i} missing 'id'/'segment_id'. Skipping.")
                     continue

                 try:
                     # ── PR3-3: Normalize timestamps once at the single entry point ──
                     start_time_float, end_time_float = _normalize_timestamps(
                         segment.get('start'), segment.get('end'),
                     )
                     text_content = segment.get('text') or ""
                     language_content = segment.get('language')
                     completed_content = bool(segment.get('completed', True))
                 except (ValueError, TypeError) as time_err:
                     logger.warning(f"[Msg {message_id}/Meet {internal_meeting_id}] Skipping segment {i} ({segment_id}) invalid time format: {time_err}")
                     continue

                 if start_time_float is None or end_time_float is None:
                     logger.warning(f"[Msg {message_id}/Meet {internal_meeting_id}] Skipping segment {i} ({segment_id}) after normalization")
                     continue

                 # Skip zero/negative duration segments
                 if end_time_float - start_time_float < 1e-3:
                     logger.debug(f"[Msg {message_id}/Meet {internal_meeting_id}] Skipping ~zero-length segment {segment_id}")
                     continue

                 # ── PR3-4: Speaker from producer; segment_id is the stable key ──
                 mapped_speaker_name = segment.get('speaker')

                 segment_redis_data = {
                     "text": text_content,
                     "start_time": start_time_float,
                     "end_time": end_time_float,
                     "language": language_content,
                     "completed": completed_content,
                     "updated_at": datetime.now(timezone.utc).isoformat(),
                     "session_uid": session_uid_from_payload,
                     "speaker": mapped_speaker_name,
                     "speaker_mapping_status": "PRODUCER_LABELED",
                     "segment_id": segment_id,
                 }

                 # Always store — no change detection needed (persistence only)
                 segments_to_store[segment_id] = json.dumps(segment_redis_data)
                 segment_count += 1

            if segment_count > 0:
                try:
                    async with redis_c.pipeline(transaction=True) as pipe:
                        pipe.sadd(f"active_meetings", str(internal_meeting_id))
                        pipe.expire(hash_key, REDIS_SEGMENT_TTL)
                        if segments_to_store:
                            pipe.hset(hash_key, mapping=segments_to_store)
                        results = await pipe.execute()
                        if any(res is None for res in results):
                            logger.error(f"Redis pipeline command failed critically for message {message_id}. Results: {results}")
                            return AckDecision.RETRY
                        logger.info(f"Stored/Updated {segment_count} segments in Redis from message {message_id} for meeting {internal_meeting_id}. Results: {results}")
                except redis.exceptions.RedisError as redis_err:
                    logger.error(f"Redis pipeline error storing segments for message {message_id}: {redis_err}", exc_info=True)
                    return AckDecision.RETRY
                except Exception as pipe_err:
                     logger.error(f"Unexpected pipeline error storing segments for message {message_id}: {pipe_err}", exc_info=True)
                     return AckDecision.RETRY

                logger.debug(f"Stored {segment_count} segments for meeting {internal_meeting_id} (persistence only, no WS publish)")
            else:
                logger.info(f"No valid segments found in message {message_id} for meeting {internal_meeting_id} to store in Redis.")
            return AckDecision.ACK

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON payload for message {message_id}: {e}. Payload: {payload_json[:200]}... Dead-lettering.")
        await _send_to_dead_letter(message_id, message_data, f"JSON parse error: {e}", redis_c)
        return AckDecision.DEAD_LETTER
    except Exception as e:
        logger.error(f"Unexpected error in process_stream_message for {message_id}: {e}", exc_info=True)
        return AckDecision.RETRY

async def process_transcript_bundle(message_id: str, stream_data: Dict[str, Any], meeting_id: int, redis_c: aioredis.Redis) -> AckDecision:
    """Process new-format transcript bundle: confirmed + pending per speaker.

    PR3: Returns an explicit AckDecision.
    """
    try:
        speaker = stream_data.get('speaker', '')
        confirmed_segs = stream_data.get('confirmed', [])
        pending_segs = stream_data.get('pending', [])
        session_uid = stream_data.get('session_uid') or stream_data.get('uid')
        hash_key = f"meeting:{meeting_id}:segments"
        now_iso = datetime.now(timezone.utc).isoformat()

        # Store confirmed segments in Redis Hash (by segment_id)
        if confirmed_segs:
            async with redis_c.pipeline(transaction=True) as pipe:
                pipe.sadd("active_meetings", str(meeting_id))
                pipe.expire(hash_key, REDIS_SEGMENT_TTL)
                for seg in confirmed_segs:
                    # ── PR3-4: Preserve upstream segment_id from 'id' or 'segment_id' ──
                    seg_id = seg.get('id') or seg.get('segment_id')
                    if not seg_id or not seg.get('text', '').strip():
                        continue
                    redis_data = {
                        "text": seg['text'], "start_time": seg.get('start', 0),
                        "end_time": seg.get('end', 0), "language": seg.get('language'),
                        "completed": True, "updated_at": now_iso,
                        "session_uid": session_uid, "speaker": seg.get('speaker', speaker),
                        "speaker_mapping_status": "PRODUCER_LABELED",
                        "segment_id": seg_id,
                    }
                    pipe.hset(hash_key, seg_id, json.dumps(redis_data))
                await pipe.execute()
            logger.info(f"[Transcript] Stored {len(confirmed_segs)} confirmed for meeting {meeting_id} speaker {speaker}")

        # Store pending snapshot (full replace per speaker, short TTL)
        pending_key = f"meeting:{meeting_id}:pending:{speaker}"
        if pending_segs:
            await redis_c.set(pending_key, json.dumps(pending_segs), ex=60)
        else:
            await redis_c.delete(pending_key)

        logger.info(f"[Transcript] Stored {len(confirmed_segs)}C + {len(pending_segs)}P for meeting {meeting_id} speaker {speaker} (persistence only)")
        return AckDecision.ACK
    except redis.exceptions.RedisError as e:
        logger.error(f"[Transcript] Redis error processing bundle {message_id}: {e}", exc_info=True)
        return AckDecision.RETRY
    except Exception as e:
        logger.error(f"[Transcript] Error processing bundle {message_id}: {e}", exc_info=True)
        return AckDecision.RETRY


async def process_speaker_event_message(message_id: str, event_data: Dict[str, Any], redis_c: aioredis.Redis) -> AckDecision:
    """Process a single speaker event message from the Redis stream.

    PR3: Returns an explicit AckDecision.
    """
    try:
        required_fields = ["uid", "relative_client_timestamp_ms", "event_type", "participant_name"]
        if not all(field in event_data for field in required_fields):
            logger.warning(f"[SpeakerProcessor] Speaker event message {message_id} missing required fields. Skipping. Data: {event_data}")
            return AckDecision.ACK

        session_uid = event_data["uid"]
        try:
            relative_timestamp_ms = float(event_data["relative_client_timestamp_ms"])
        except ValueError:
            logger.warning(f"[SpeakerProcessor] Invalid relative_client_timestamp_ms '{event_data['relative_client_timestamp_ms']}' for message {message_id}. Skipping.")
            return AckDecision.ACK

        event_payload_json = json.dumps(event_data)
        sorted_set_key = f"{REDIS_SPEAKER_EVENT_KEY_PREFIX}:{session_uid}"

        async with redis_c.pipeline(transaction=True) as pipe:
            pipe.zadd(sorted_set_key, {event_payload_json: relative_timestamp_ms})
            pipe.expire(sorted_set_key, REDIS_SPEAKER_EVENT_TTL)
            results = await pipe.execute()

        logger.debug(f"[SpeakerProcessor] Stored speaker event for UID '{session_uid}' at {relative_timestamp_ms}ms. Key: {sorted_set_key}. Message ID: {message_id}")
        return AckDecision.ACK

    except redis.exceptions.RedisError as e_redis:
        logger.error(f"[SpeakerProcessor] Redis error processing speaker event message {message_id}: {e_redis}", exc_info=True)
        return AckDecision.RETRY
    except Exception as e:
        logger.error(f"[SpeakerProcessor] Unexpected error in process_speaker_event_message for {message_id}: {e}", exc_info=True)
        return AckDecision.RETRY
