"""Post-meeting tasks — aggregation, webhooks, hooks.

Post-meeting aggregation, webhooks, and hooks.
Same logic, same webhook payloads.
"""

import enum
import logging
import os

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Meeting, Transcription
from .database import async_session_local
from .webhook_delivery import deliver, build_envelope

from .config import TRANSCRIPTION_COLLECTOR_URL, POST_MEETING_HOOKS
from .webhooks import send_completion_webhook

logger = logging.getLogger("meeting_api.post_meeting")


# ---------------------------------------------------------------------------
# Post-meeting state machine
# ---------------------------------------------------------------------------
# WAITING_FOR_ARTIFACTS -> TRANSCRIPT_READY -> SPEAKERS_READY -> NOTES_READY -> COMPLETE
#
# Each transition is idempotent: re-running a finished stage is a no-op.
# Failed stages record the error and schedule bounded retry.

class PostMeetingState(enum.Enum):
    WAITING_FOR_ARTIFACTS = "waiting_for_artifacts"
    TRANSCRIPT_READY = "transcript_ready"
    SPEAKERS_READY = "speakers_ready"
    NOTES_READY = "notes_ready"
    COMPLETE = "complete"


_MAX_RETRIES = 3
_RETRY_KEY = "post_meeting_state"
_ATTEMPT_KEY = "attempt_count"
_ERROR_KEY = "last_error_class"
_ERROR_MSG_KEY = "last_error_message"
_STAGE_TS_KEY = "post_meeting_stage_timestamps"
_PREV_NOTES_KEY = "ai_notes_previous"
_PROMPT_VER_KEY = "ai_notes_prompt_version"


def _get_pm_state(meeting: Meeting) -> str:
    """Return the current post-meeting pipeline state (default: WAITING_FOR_ARTIFACTS)."""
    return (meeting.data or {}).get(_RETRY_KEY, PostMeetingState.WAITING_FOR_ARTIFACTS.value)


def _set_pm_state(meeting: Meeting, state: str, attempt: int = 0,
                  error_class: str = None, error_msg: str = None) -> None:
    """Update persisted post-meeting state metadata."""
    from datetime import datetime
    from sqlalchemy.orm.attributes import flag_modified
    data = dict(meeting.data or {})
    data[_RETRY_KEY] = state
    data[_ATTEMPT_KEY] = attempt
    if error_class:
        data[_ERROR_KEY] = error_class
        data[_ERROR_MSG_KEY] = error_msg
    else:
        data.pop(_ERROR_KEY, None)
        data.pop(_ERROR_MSG_KEY, None)
    timestamps = dict(data.get(_STAGE_TS_KEY, {}))
    timestamps[state] = datetime.utcnow().isoformat()
    data[_STAGE_TS_KEY] = timestamps
    meeting.data = data
    flag_modified(meeting, "data")


def _record_failure(meeting: Meeting, error: Exception) -> None:
    """Record failed stage metadata for retry bookkeeping."""
    from datetime import datetime
    from sqlalchemy.orm.attributes import flag_modified
    data = dict(meeting.data or {})
    data[_ATTEMPT_KEY] = (data.get(_ATTEMPT_KEY) or 0) + 1
    data[_ERROR_KEY] = type(error).__name__
    data[_ERROR_MSG_KEY] = str(error)[:500]
    meeting.data = data
    flag_modified(meeting, "data")


async def _final_segment_count(db: AsyncSession, meeting_id: int) -> int:
    result = await db.execute(
        select(func.count(Transcription.id)).where(
            Transcription.meeting_id == meeting_id,
            Transcription.status == "final",
        )
    )
    return int(result.scalar() or 0)


# v0.10.5 Pack H — aggregation_failure_class taxonomy.
#
# JSONB discriminator (NO PG migration; meetings.data is JSONB and
# meetings.status is String(50) — both already accept new keys/values
# without schema changes). Three values:
#   - "transient_infra"      — tx-gateway 5xx; retry-eligible via Pack H.4 sweep
#   - "permanent_infra"      — 7-day retry budget exhausted; terminal; alerts critical
#   - "user_meaningful"      — bot crash, validation error, etc; terminal as `status=failed`
#
# Single-write-path discipline: every write goes through
# set_aggregation_failure_class() helper. Pack H's registry check
# AGGREGATION_FAILURE_CLASS_VIA_TYPED_HELPER asserts no other call site
# touches data['aggregation_failure_class'] directly.
class AggregationFailureClass:
    TRANSIENT_INFRA = "transient_infra"
    PERMANENT_INFRA = "permanent_infra"
    USER_MEANINGFUL = "user_meaningful"


def set_aggregation_failure_class(meeting: Meeting, cls: str) -> None:
    """Canonical single-write-path for data.aggregation_failure_class.

    Updates meeting.data dict in place + flags it modified for SQLAlchemy.
    Caller commits.
    """
    valid = {
        AggregationFailureClass.TRANSIENT_INFRA,
        AggregationFailureClass.PERMANENT_INFRA,
        AggregationFailureClass.USER_MEANINGFUL,
    }
    if cls not in valid:
        raise ValueError(f"Invalid aggregation_failure_class: {cls!r}; must be one of {valid}")
    data = dict(meeting.data or {})
    data["aggregation_failure_class"] = cls
    from datetime import datetime
    data["aggregation_last_retry_at"] = datetime.utcnow().isoformat()
    data["aggregation_retry_count"] = (data.get("aggregation_retry_count") or 0) + 1
    meeting.data = data
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(meeting, "data")


def clear_aggregation_failure_class(meeting: Meeting) -> None:
    """Clear failure_class on success — also via the canonical write path."""
    if not meeting.data:
        return
    data = dict(meeting.data)
    if "aggregation_failure_class" in data:
        del data["aggregation_failure_class"]
        meeting.data = data
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(meeting, "data")


async def aggregate_transcription(meeting: Meeting, db: AsyncSession):
    """Fetch transcription segments and aggregate into meeting.data.

    v0.10.5 Pack H — distinguishes transient infra failures (5xx, network
    error) from permanent failures (4xx auth/validation) from success.
    Pre-Pack-H: ALL non-200 returned silently; meeting marked `failed`
    via callers' default-to-completed/failed paths regardless of cause.
    Real-world incident 2026-04-23: tx-gateway pod restart during
    aggregate → 23 consecutive meetings marked `failed` from a transient
    flap.

    Now:
      - 5xx OR network error → set aggregation_failure_class='transient_infra'
        — retry-eligible via Pack H.4 sweep in sweeps.py (24 retries × exp
        backoff, 7-day budget). Caller stays in non-terminal state until
        either retry succeeds or budget exhausts.
      - 4xx → set aggregation_failure_class='permanent_infra' (e.g. auth
        misconfig); terminal; alerts critical. Operator action required.
      - 200 + segments → clear failure_class; aggregate normally.

    Returns True on terminal success, False on transient (caller can choose
    to surface aggregation_failed event vs leave for sweep retry).
    """
    meeting_id = meeting.id
    try:
        collector_url = f"{TRANSCRIPTION_COLLECTOR_URL}/internal/transcripts/{meeting_id}"
        internal_secret = os.getenv("INTERNAL_API_SECRET", "")
        headers = {"X-Internal-Secret": internal_secret} if internal_secret else {}
        async with httpx.AsyncClient() as client:
            response = await client.get(collector_url, timeout=30.0, headers=headers)

        # v0.10.5 Pack H — distinguish 5xx (transient) from 4xx (permanent).
        if 500 <= response.status_code < 600:
            logger.warning(
                f"Pack H: tx-gateway returned {response.status_code} for meeting {meeting_id} "
                f"— transient infra, retrying via sweep"
            )
            set_aggregation_failure_class(meeting, AggregationFailureClass.TRANSIENT_INFRA)
            await db.commit()
            return False
        if response.status_code != 200:
            # 4xx — permanent. Auth misconfig, malformed request, etc.
            logger.error(
                f"Pack H: tx-gateway returned {response.status_code} for meeting {meeting_id} "
                f"— permanent infra failure (operator action required)"
            )
            set_aggregation_failure_class(meeting, AggregationFailureClass.PERMANENT_INFRA)
            await db.commit()
            return False

        segments = response.json()
        if not segments:
            # Empty result is legitimate (zero-segment meeting); clear any
            # prior failure_class to indicate aggregation completed cleanly.
            clear_aggregation_failure_class(meeting)
            await db.commit()
            return True

        unique_speakers = set()
        unique_languages = set()
        for seg in segments:
            speaker = seg.get("speaker")
            language = seg.get("language")
            if speaker and speaker.strip():
                unique_speakers.add(speaker.strip())
            if language and language.strip():
                unique_languages.add(language.strip())

        existing_data = meeting.data or {}
        changed = False
        if "participants" not in existing_data and unique_speakers:
            existing_data["participants"] = sorted(unique_speakers)
            changed = True
        if "languages" not in existing_data and unique_languages:
            existing_data["languages"] = sorted(unique_languages)
            changed = True

        if changed:
            meeting.data = existing_data
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(meeting, "data")
            logger.info(
                f"Aggregated transcription data for meeting {meeting_id}: "
                f"{len(segments)} segments, {len(unique_speakers)} speakers, "
                f"languages={sorted(unique_languages)}"
            )
        else:
            logger.info(
                f"Aggregated transcription for meeting {meeting_id}: "
                f"{len(segments)} segments (no metadata changes)"
            )

        # Success — clear any prior transient failure_class.
        clear_aggregation_failure_class(meeting)
        await db.commit()
        return True

    except httpx.RequestError as exc:
        # Network error (DNS, TLS, timeout) — same class as 5xx; retry-eligible.
        logger.warning(
            f"Pack H: tx-gateway request error for meeting {meeting_id}: "
            f"{type(exc).__name__}: {exc!r} — transient infra, retrying via sweep"
        )
        set_aggregation_failure_class(meeting, AggregationFailureClass.TRANSIENT_INFRA)
        try:
            await db.commit()
        except Exception:
            pass
        return False
    except Exception as e:
        # Unknown error — log loudly + don't mark transient (don't retry into a code bug).
        logger.error(
            f"Pack H: aggregation failed for meeting {meeting_id}: "
            f"{type(e).__name__}: {e!r}",
            exc_info=True,
        )
        return False


async def fire_post_meeting_hooks(meeting: Meeting, db: AsyncSession):
    """Fire POST_MEETING_HOOKS to configured internal services (billing, analytics, etc.)."""
    if not POST_MEETING_HOOKS:
        return

    if not meeting.start_time or not meeting.end_time:
        return

    # Resolve real email from users table — billing hooks need it to meter usage
    try:
        from admin_models.models import User
        user = (await db.execute(select(User).where(User.id == meeting.user_id))).scalars().first()
        if not user or not user.email:
            logger.error(f"Cannot resolve email for user {meeting.user_id} — skipping billing hook")
            return
        user_email = user.email
    except Exception as e:
        logger.error(f"DB error resolving email for user {meeting.user_id} — skipping billing hook: {e}")
        return

    duration_seconds = (meeting.end_time - meeting.start_time).total_seconds()
    meeting_data = meeting.data or {}

    payload = build_envelope("meeting.completed", {
        "meeting": {
            "id": meeting.id,
            "user_id": meeting.user_id,
            "user_email": user_email,
            "platform": meeting.platform,
            "status": meeting.status,
            "duration_seconds": duration_seconds,
            "start_time": meeting.start_time.isoformat(),
            "end_time": meeting.end_time.isoformat(),
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
            "transcription_enabled": meeting_data.get("transcribe_enabled", False),
        },
    })

    for hook_url in POST_MEETING_HOOKS:
        await deliver(
            url=hook_url,
            payload=payload,
            timeout=10.0,
            label=f"post-meeting-hook meeting={meeting.id}",
        )


async def finalize_in_progress_recordings(meeting: Meeting, db: AsyncSession) -> int:
    """Mark all IN_PROGRESS recordings as COMPLETED + flip media_files[*].is_final=true.

    v0.10.5 (post-prod-telemetry 2026-04-30) — Bug B: pre-fix, recordings whose
    finalizer chunk never reached the server (bot was killed before it could send
    the empty-body is_final=true chunk) stayed IN_PROGRESS forever, with all
    media_files entries showing is_final=false. Consumers polling for is_final
    couldn't tell when the recording is truly done.

    Now: at post-meeting time (after meeting is in terminal state), any rec
    payload still IN_PROGRESS gets flipped to COMPLETED, and every media_files
    entry's is_final flag flipped to true. The actual chunk files in MinIO are
    already there; this is purely the metadata reconciliation.

    Returns count of recordings that were finalized here (0 if everything was
    already finalized via the canonical chunk-finalizer path).
    """
    from sqlalchemy.orm import attributes
    from .schemas import RecordingStatus
    from datetime import datetime as _dt

    if not meeting or not meeting.data:
        return 0
    recordings_list = list((meeting.data or {}).get("recordings") or [])
    if not recordings_list:
        return 0

    finalized_count = 0
    changed = False
    for idx, rec in enumerate(recordings_list):
        if not isinstance(rec, dict):
            continue
        # Only finalize recordings that haven't been completed via the
        # canonical chunk-finalizer path. Already-completed recordings stay
        # untouched.
        if rec.get("status") == RecordingStatus.COMPLETED.value:
            continue
        rec_payload = dict(rec)
        rec_payload["status"] = RecordingStatus.COMPLETED.value
        rec_payload["completed_at"] = rec_payload.get("completed_at") or _dt.utcnow().isoformat()
        # Flip is_final on every media_files entry so consumers see the
        # recording as done.
        media_files = list(rec_payload.get("media_files") or [])
        any_changed = False
        for mf in media_files:
            if not isinstance(mf, dict):
                continue
            if not mf.get("is_final"):
                mf["is_final"] = True
                mf["finalized_at"] = _dt.utcnow().isoformat()
                mf["finalized_by"] = "post_meeting_reconciler"
                any_changed = True
        rec_payload["media_files"] = media_files
        recordings_list[idx] = rec_payload
        finalized_count += 1
        changed = changed or any_changed or True

    if changed:
        meeting_data_dict = dict(meeting.data or {})
        meeting_data_dict["recordings"] = recordings_list
        meeting.data = meeting_data_dict
        attributes.flag_modified(meeting, "data")
        logger.info(
            "[Bug-B-Fix] post_meeting_reconciler finalized recordings for meeting %s: count=%s",
            meeting.id, finalized_count,
        )
    return finalized_count


async def run_all_tasks(meeting_id: int):
    """State-machine driven post-meeting pipeline.

    WAITING_FOR_ARTIFACTS -> TRANSCRIPT_READY -> SPEAKERS_READY -> NOTES_READY -> COMPLETE

    Each stage is idempotent: re-running a completed stage is a no-op.
    Failed stages record the error and schedule bounded retry (up to _MAX_RETRIES).
    """
    logger.info(f"Starting post-meeting pipeline for meeting {meeting_id}")

    async with async_session_local() as db:
        meeting = await db.get(Meeting, meeting_id)
        if not meeting:
            logger.error(f"Meeting {meeting_id} not found for post-meeting tasks")
            return

        current = _get_pm_state(meeting)
        attempt = (meeting.data or {}).get(_ATTEMPT_KEY, 0) or 0

        # --- Stage 1: Recording finalization + transcription aggregation ---
        if current in (PostMeetingState.WAITING_FOR_ARTIFACTS.value,
                       PostMeetingState.TRANSCRIPT_READY.value):
            try:
                count = await finalize_in_progress_recordings(meeting, db)
                if count > 0:
                    await db.commit()
                ok = await aggregate_transcription(meeting, db)
                if not ok:
                    failure = (meeting.data or {}).get("aggregation_failure_class")
                    if failure == AggregationFailureClass.TRANSIENT_INFRA:
                        if attempt >= _MAX_RETRIES:
                            set_aggregation_failure_class(
                                meeting, AggregationFailureClass.PERMANENT_INFRA)
                            await db.commit()
                            logger.error(
                                f"Transient retries exhausted for meeting {meeting_id}")
                            return
                        _record_failure(meeting, Exception("transient infra"))
                        await db.commit()
                        logger.warning(
                            f"Retrying transcription for meeting {meeting_id} "
                            f"(attempt {attempt + 1}/{_MAX_RETRIES})")
                        return
                    elif failure == AggregationFailureClass.PERMANENT_INFRA:
                        _record_failure(meeting, Exception("permanent infra"))
                        await db.commit()
                        return
                final_segments = await _final_segment_count(db, meeting_id)
                if final_segments == 0:
                    _record_failure(meeting, RuntimeError("no final transcript segments"))
                    await db.commit()
                    logger.warning(
                        "Stage TRANSCRIPT_READY waiting for final transcript segments for meeting %s",
                        meeting_id,
                    )
                    return

                _set_pm_state(meeting, PostMeetingState.TRANSCRIPT_READY.value)
                await db.commit()
            except Exception as e:
                _record_failure(meeting, e)
                await db.commit()
                logger.error(
                    f"Stage TRANSCRIPT_READY failed for meeting {meeting_id}: {e}",
                    exc_info=True)
                return

        current = _get_pm_state(meeting)

        # --- Stage 2: Speaker diarization (requires finalized transcripts) ---
        if current in (PostMeetingState.TRANSCRIPT_READY.value,
                       PostMeetingState.SPEAKERS_READY.value):
            try:
                from .diarization_service import run_diarization
                result = await run_diarization(meeting_id, db)
                _set_pm_state(meeting, PostMeetingState.SPEAKERS_READY.value)
                await db.commit()
                logger.info(
                    f"Stage SPEAKERS_READY for meeting {meeting_id}: "
                    f"{result.get('total_speakers', 0)} speakers")
            except Exception as e:
                _record_failure(meeting, e)
                await db.commit()
                logger.error(
                    f"Stage SPEAKERS_READY failed for meeting {meeting_id}: {e}",
                    exc_info=True)
                return

        current = _get_pm_state(meeting)

        # --- Stage 3: AI notes (requires at least one finalized segment) ---
        if current in (PostMeetingState.SPEAKERS_READY.value,
                       PostMeetingState.NOTES_READY.value):
            try:
                from .meeting_intelligence import generate_ai_notes
                result = await generate_ai_notes(meeting_id)
                if result:
                    # generate_ai_notes persists through its own DB session. Reload
                    # JSONB before advancing state so this session cannot overwrite
                    # the newly generated notes with its stale meeting.data value.
                    await db.refresh(meeting, attribute_names=["data"])
                    _set_pm_state(meeting, PostMeetingState.NOTES_READY.value)
                    await db.commit()
                else:
                    from .intelligence_config import AI_NOTES_ENABLED
                    if AI_NOTES_ENABLED:
                        raise RuntimeError("AI notes generation failed")
                    _set_pm_state(meeting, PostMeetingState.NOTES_READY.value)
                    await db.commit()
                    logger.info(f"AI notes disabled for meeting {meeting_id}")
            except Exception as e:
                _record_failure(meeting, e)
                await db.commit()
                logger.error(
                    f"Stage NOTES_READY failed for meeting {meeting_id}: {e}",
                    exc_info=True)
                return

        current = _get_pm_state(meeting)

        # --- Stage 4: Webhook + hooks (export/search readiness) ---
        if current != PostMeetingState.COMPLETE.value:
            try:
                await send_completion_webhook(meeting, db)
                await fire_post_meeting_hooks(meeting, db)
                _set_pm_state(meeting, PostMeetingState.COMPLETE.value)
                await db.commit()
            except Exception as e:
                _record_failure(meeting, e)
                await db.commit()
                logger.error(
                    f"Stage COMPLETE failed for meeting {meeting_id}: {e}",
                    exc_info=True)
                return

    logger.info(f"Post-meeting pipeline COMPLETE for meeting {meeting_id}")


async def run_status_webhook_task(meeting_id: int, status_change_info: dict = None):
    """Run status webhook — short-lived DB session, HTTP call outside session."""
    from .webhooks import send_status_webhook

    try:
        async with async_session_local() as db:
            meeting = await db.get(Meeting, meeting_id)
            if not meeting:
                logger.error(f"Meeting {meeting_id} not found for status webhook")
                return
            await send_status_webhook(meeting, db, status_change_info)
            await db.commit()
    except Exception as e:
        logger.error(f"Error in status webhook for meeting {meeting_id}: {e}", exc_info=True)
