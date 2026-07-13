"""Meeting Intelligence — AI-powered post-meeting note generation.

Triggers on meeting.completed, fetches transcripts, calls LLM,
and persists structured notes into meeting.data['ai_notes'].
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import List, Optional

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from .database import async_session_local
from .models import Meeting, Transcription
from .intelligence_config import (
    AI_MODEL,
    AI_API_KEY,
    AI_BASE_URL,
    AI_API_VERSION,
    AI_NOTES_ENABLED,
    AI_NOTES_SYSTEM_PROMPT,
    AI_NOTES_TIMEOUT,
    MAX_TRANSCRIPT_TOKENS,
)


# ---------------------------------------------------------------------------
# Pydantic model for validated AI notes output
# ---------------------------------------------------------------------------

class AIMoment(BaseModel):
    timestamp: str = Field(description="MM:SS timestamp in transcript")
    speaker: str = Field(description="speaker name or 'unknown'")
    text: str = Field(description="what was said")


class AIActionItem(BaseModel):
    description: str = Field(description="what to do")
    assignee: str = Field(description="who (or 'unassigned')")
    deadline: Optional[str] = Field(default=None, description="date if mentioned")


class AINotes(BaseModel):
    summary: str = Field(description="3-5 sentence meeting summary")
    key_moments: List[AIMoment] = Field(default_factory=list)
    decisions: List[str] = Field(default_factory=list)
    action_items: List[AIActionItem] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list, description="open questions")


# Prompt version — bump when the system prompt changes so cached notes
# are regenerated with the new prompt.
AI_NOTES_PROMPT_VERSION = "1"

logger = logging.getLogger("meeting_api.meeting_intelligence")


def _resolve_provider_config():
    """Resolve provider/base_url from AI_MODEL (same logic as dashboard route.ts)."""
    if not AI_MODEL:
        return None, None, None
    parts = AI_MODEL.split("/", 1)
    if len(parts) != 2:
        return None, None, None
    provider, model = parts[0].lower(), parts[1]
    api_key = AI_API_KEY or "not-needed"
    base_url = AI_BASE_URL

    provider_urls = {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "groq": "https://api.groq.com/openai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": "http://localhost:11434/v1",
        "local": "http://localhost:11434/v1",
        "custom": base_url or "http://localhost:11434/v1",
    }

    if provider in provider_urls:
        base_url = base_url or provider_urls[provider]

    if provider == "anthropic":
        # Anthropic uses /v1/messages, not /v1/chat/completions
        endpoint = f"{base_url.rstrip('/')}/v1/messages"
        return "anthropic", model, endpoint
    else:
        # OpenAI-compatible
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        return "openai-compatible", model, endpoint


def _build_transcript_text(segments):
    """Build a plain-text transcript from Transcription segments, sorted by time."""
    sorted_segs = sorted(segments, key=lambda s: s.start_time)
    lines = []
    for seg in sorted_segs:
        mins, secs = divmod(int(seg.start_time), 60)
        ts = f"{mins:02d}:{secs:02d}"
        speaker = f" [{seg.speaker}]" if seg.speaker else ""
        lines.append(f"{ts}{speaker}: {seg.text}")
    return "\n".join(lines)


def _compute_fingerprint(segments) -> str:
    """Compute a SHA-256 fingerprint from transcript segment IDs + text.

    Used to detect whether the underlying transcript has changed so we can
    skip regeneration when nothing meaningful has shifted.
    """
    h = hashlib.sha256()
    sorted_segs = sorted(segments, key=lambda s: s.start_time)
    for seg in sorted_segs:
        h.update(f"{seg.segment_id or seg.id}:{seg.text}".encode("utf-8"))
    return h.hexdigest()


async def fetch_transcripts_for_meeting(meeting_id: int):
    """Fetch finalized Transcription rows for a meeting, ordered by start_time."""
    async with async_session_local() as db:
        result = await db.execute(
            select(Transcription)
            .where(Transcription.meeting_id == meeting_id)
            .where(Transcription.status == "final")
            .order_by(Transcription.start_time)
        )
        return result.scalars().all()


async def generate_ai_notes(meeting_id: int):
    """Generate AI notes for a completed meeting and persist to meeting.data.

    - Requires at least one finalized transcript segment.
    - Skips regeneration when the stored fingerprint matches current segments
      and the prompt version has not changed.
    - Validates LLM output with AINotes Pydantic model.
    - Stores model, prompt version, transcript fingerprint and generation
      timestamp beside ai_notes.
    - Keeps previous valid notes when regeneration fails.

    Returns True if notes were generated and saved, False otherwise.
    """
    if not AI_NOTES_ENABLED:
        logger.info("AI notes generation not configured (set AI_MODEL + AI_API_KEY/AI_BASE_URL)")
        return False

    provider, model, endpoint = _resolve_provider_config()
    if not provider:
        logger.error("AI_MODEL is not set or has invalid format (expected provider/model)")
        return False

    # Fetch transcripts (only finalized)
    segments = await fetch_transcripts_for_meeting(meeting_id)
    if not segments:
        logger.info(f"No finalized transcript segments for meeting {meeting_id}, skipping AI notes")
        return False

    # Compute fingerprint to detect transcript changes
    fingerprint = _compute_fingerprint(segments)

    # Check if regeneration is needed
    async with async_session_local() as db:
        meeting = await db.get(Meeting, meeting_id)
        if not meeting:
            logger.error(f"Meeting {meeting_id} not found for AI notes")
            return False

        stored_fp = (meeting.data or {}).get("ai_notes_fingerprint")
        stored_pv = (meeting.data or {}).get("ai_notes_prompt_version")
        if (
            fingerprint == stored_fp
            and stored_pv == AI_NOTES_PROMPT_VERSION
            and (meeting.data or {}).get("ai_notes")
        ):
            logger.info(
                f"Transcript fingerprint unchanged for meeting {meeting_id} — "
                f"skipping AI notes regeneration"
            )
            return True

    transcript_text = _build_transcript_text(segments)
    logger.info(
        f"Generating AI notes for meeting {meeting_id}: "
        f"{len(segments)} segments, {len(transcript_text)} chars, provider={provider}, model={model}"
    )

    # Truncate if needed (rough char-based cap; tokens ~ 4 chars each)
    max_chars = MAX_TRANSCRIPT_TOKENS * 4
    if len(transcript_text) > max_chars:
        transcript_text = transcript_text[:max_chars] + "\n...(truncated)"

    # Build the request
    user_message = f"{AI_NOTES_SYSTEM_PROMPT}\n\nTRANSCRIPT:\n{transcript_text}"

    if provider == "anthropic":
        req_body = _build_anthropic_request(model, user_message)
        headers = _get_anthropic_headers()
    else:
        req_body = _build_openai_request(model, user_message)
        headers = _get_openai_headers()

    try:
        async with httpx.AsyncClient(timeout=AI_NOTES_TIMEOUT) as client:
            resp = await client.post(endpoint, json=req_body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Extract response text
        if provider == "anthropic":
            ai_text = data.get("content", [{}])[0].get("text", "")
        else:
            ai_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Parse + validate through Pydantic model
        raw = _parse_ai_notes(ai_text)
        if not raw:
            raise ValueError("AI returned empty or unparseable JSON")

        notes_model = AINotes.model_validate(raw)
        notes_dict = notes_model.model_dump()

        # Persist to meeting.data — back up previous notes before overwriting
        async with async_session_local() as db:
            meeting = await db.get(Meeting, meeting_id)
            if not meeting:
                logger.error(f"Meeting {meeting_id} not found for saving AI notes")
                return False

            data_dict = dict(meeting.data or {})
            prev = data_dict.get("ai_notes")
            if prev:
                data_dict["ai_notes_previous"] = prev
            data_dict["ai_notes"] = notes_dict
            data_dict["ai_notes_generated_at"] = datetime.utcnow().isoformat()
            data_dict["ai_notes_model"] = f"{provider}/{model}"
            data_dict["ai_notes_prompt_version"] = AI_NOTES_PROMPT_VERSION
            data_dict["ai_notes_fingerprint"] = fingerprint
            meeting.data = data_dict
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(meeting, "data")
            await db.commit()

        logger.info(
            f"AI notes saved for meeting {meeting_id}: "
            f"summary={len(notes_dict.get('summary', ''))} chars, "
            f"moments={len(notes_dict.get('key_moments', []))}, "
            f"decisions={len(notes_dict.get('decisions', []))}, "
            f"action_items={len(notes_dict.get('action_items', []))}"
        )
        return True

    except httpx.RequestError as e:
        await _keep_previous_notes(meeting_id)
        logger.error(f"Network error generating AI notes for meeting {meeting_id}: {e}")
        return False
    except Exception as e:
        await _keep_previous_notes(meeting_id)
        logger.error(f"Error generating AI notes for meeting {meeting_id}: {e}", exc_info=True)
        return False


def _build_openai_request(model, user_text):
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an expert meeting analyst. Return ONLY valid JSON."},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }


def _build_anthropic_request(model, user_text):
    return {
        "model": model,
        "system": "You are an expert meeting analyst. Return ONLY valid JSON.",
        "messages": [{"role": "user", "content": user_text}],
        "temperature": 0.3,
        "max_tokens": 8000,
    }


def _get_openai_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}",
    }


def _get_anthropic_headers():
    return {
        "Content-Type": "application/json",
        "x-api-key": AI_API_KEY,
        "anthropic-version": "2023-06-01",
    }


def _parse_ai_notes(ai_text: str) -> dict | None:
    """Parse the AI response, stripping markdown code fences if present."""
    text = ai_text.strip()
    # Strip ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse AI notes JSON: {text[:200]}")
        return None


async def _keep_previous_notes(meeting_id: int) -> None:
    """Restore previous valid notes when regeneration fails."""
    async with async_session_local() as db:
        meeting = await db.get(Meeting, meeting_id)
        if not meeting:
            return
        data_dict = dict(meeting.data or {})
        prev = data_dict.get("ai_notes_previous")
        if prev:
            data_dict["ai_notes"] = prev
            meeting.data = data_dict
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(meeting, "data")
            await db.commit()
            logger.info(f"Restored previous AI notes for meeting {meeting_id} after failure")
