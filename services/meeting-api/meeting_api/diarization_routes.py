"""Diarization routes — speaker identification with adapter-based backends."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .diarization_service import (
    DiarizationBackend,
    StubDiarizationBackend,
    run_diarization,
)

router = APIRouter()


class DiarizeRequest(BaseModel):
    """Request body for audio-based diarization."""

    audio_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Backend registry — swap StubDiarizationBackend for real implementation
# ---------------------------------------------------------------------------

_diarization_backend: Optional[DiarizationBackend] = StubDiarizationBackend()


def set_diarization_backend(backend: DiarizationBackend) -> None:
    """Replace the active diarization backend (e.g., at startup)."""
    global _diarization_backend
    _diarization_backend = backend


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/internal/meetings/{meeting_id}/diarize")
async def diarize_meeting(
    meeting_id: int,
    request: Optional[DiarizeRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Run speaker diarization on a meeting's transcript segments.

    With no body: returns current segment speaker state (from platform
    speaker-event pipeline via speaker_mapper).

    With *audio_path*: runs audio-based diarization through the configured
    backend and aligns turns to segments by maximum timestamp overlap.
    """
    audio_path = None
    backend = _diarization_backend

    if request and request.audio_path:
        if not backend:
            raise HTTPException(
                status_code=501,
                detail="No diarization backend configured; audio fallback unavailable",
            )
        audio_path = Path(request.audio_path)
        if not audio_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Audio file not found: {audio_path}",
            )

    return await run_diarization(
        meeting_id=meeting_id,
        db=db,
        backend=backend,
        audio_path=audio_path,
    )
