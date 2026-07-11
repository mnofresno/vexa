"""Speaker diarization adapter — Protocol-based backend interface."""

import logging
from pathlib import Path
from typing import Protocol, List, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Transcription

logger = logging.getLogger("meeting_api.diarization")


@runtime_checkable
class SpeakerTurn(Protocol):
    """A single speaker turn returned by a diarization backend."""

    @property
    def start(self) -> float:
        ...

    @property
    def end(self) -> float:
        ...

    @property
    def speaker_label(self) -> str:
        ...

    @property
    def confidence(self) -> float:
        ...


@runtime_checkable
class DiarizationBackend(Protocol):
    """Adapter interface for audio-based speaker diarization backends.

    Implementations:
    - StubBackend (current — placeholder until real embedding backend)
    - PyannoteBackend (future)
    - NeMoBackend (future)
    """

    async def diarize(self, audio_path: Path) -> List[SpeakerTurn]:
        ...


class StubSpeakerTurn:
    """Minimal SpeakerTurn implementation for the stub backend."""

    def __init__(self, start: float, end: float, label: str, confidence: float):
        self._start = start
        self._end = end
        self._label = label
        self._confidence = confidence

    @property
    def start(self) -> float:
        return self._start

    @property
    def end(self) -> float:
        return self._end

    @property
    def speaker_label(self) -> str:
        return self._label

    @property
    def confidence(self) -> float:
        return self._confidence


class StubDiarizationBackend:
    """Stub diarization backend — returns empty turns.

    This is a placeholder until a real embedding-based backend
    (pyannote/NeMo) is implemented. It never infers real participant
    identity from voice without enrollment.
    """

    async def diarize(self, audio_path: Path) -> List[SpeakerTurn]:
        logger.warning(
            "StubDiarizationBackend called (no real backend configured): %s",
            audio_path,
        )
        return []


# ---------------------------------------------------------------------------
# Audio-turn to transcript-segment alignment
# ---------------------------------------------------------------------------

def align_turns_to_segments(
    segments: List[Transcription],
    turns: List[SpeakerTurn],
) -> List[dict]:
    """Align audio diarization turns to transcript segments by max timestamp overlap.

    Returns a list of dicts with segment reference and assigned turn info.
    Segments without overlapping turns are returned with no speaker assignment.
    """
    results: List[dict] = []
    for seg in segments:
        best: dict | None = None
        best_overlap = 0.0
        for turn in turns:
            overlap_start = max(turn.start, seg.start_time)
            overlap_end = min(turn.end, seg.end_time)
            overlap = max(0, overlap_end - overlap_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best = {
                    "speaker_label": turn.speaker_label,
                    "confidence": turn.confidence,
                }
        results.append({
            "segment": seg,
            "turn": best if best and best_overlap > 0 else None,
        })
    return results


# ---------------------------------------------------------------------------
# Orchestrator — runs diarization for a meeting
# ---------------------------------------------------------------------------

async def run_diarization(
    meeting_id: int,
    db: AsyncSession,
    backend: DiarizationBackend | None = None,
    audio_path: Path | None = None,
) -> dict:
    """Run speaker diarization on meeting transcript segments.

    If *backend* and *audio_path* are provided, audio-based diarization
    is used to assign anonymous speaker turns to segments.
    Otherwise, segments with no speaker already set remain unassigned
    (the caller's speaker_mapper layer should have populated them).
    """
    segments = (await db.execute(
        select(Transcription).where(
            Transcription.meeting_id == meeting_id
        ).order_by(Transcription.start_time)
    )).scalars().all()

    if not segments:
        return {"total_speakers": 0, "segments": [], "updated_count": 0}

    updated = 0

    if backend and audio_path:
        turns = await backend.diarize(audio_path)
        alignments = align_turns_to_segments(segments, turns)
        for aligned in alignments:
            seg = aligned["segment"]
            turn = aligned["turn"]
            if turn and (not seg.speaker or seg.speaker == "unknown"):
                seg.speaker = turn["speaker_label"]
                updated += 1
        if updated:
            await db.commit()
        label_set = {t.speaker_label for t in turns}
        return {
            "total_speakers": len(label_set),
            "segments": _format_diarization_response(segments),
            "updated_count": updated,
        }

    # No audio backend — return current segment state.
    # Speaker attribution is expected from the platform speaker-event
    # pipeline (speaker_mapper).
    return {
        "total_speakers": len({s.speaker for s in segments if s.speaker}),
        "segments": _format_diarization_response(segments),
        "updated_count": 0,
    }


def _format_diarization_response(segments: List[Transcription]) -> list:
    result = []
    for seg in segments:
        result.append({
            "start": round(seg.start_time, 2),
            "end": round(seg.end_time, 2),
            "speaker": seg.speaker,
        })
    return result
