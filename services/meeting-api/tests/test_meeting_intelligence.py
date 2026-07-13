from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from meeting_api import meeting_intelligence


class _SessionContext:
    def __init__(self, meeting):
        self._meeting = meeting

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, _model, _meeting_id):
        return self._meeting


@pytest.mark.asyncio
async def test_generate_ai_notes_treats_current_notes_as_success():
    segment = SimpleNamespace(id=1, segment_id="seg-1", text="hello", start_time=0.0)
    fingerprint = meeting_intelligence._compute_fingerprint([segment])
    meeting = SimpleNamespace(data={
        "ai_notes": {"summary": "Existing notes"},
        "ai_notes_fingerprint": fingerprint,
        "ai_notes_prompt_version": meeting_intelligence.AI_NOTES_PROMPT_VERSION,
    })

    with (
        patch.object(meeting_intelligence, "AI_NOTES_ENABLED", True),
        patch.object(meeting_intelligence, "AI_MODEL", "local/test-model"),
        patch.object(meeting_intelligence, "AI_BASE_URL", "http://localhost:11434/v1"),
        patch.object(
            meeting_intelligence,
            "fetch_transcripts_for_meeting",
            new=AsyncMock(return_value=[segment]),
        ),
        patch.object(
            meeting_intelligence,
            "async_session_local",
            return_value=_SessionContext(meeting),
        ),
    ):
        assert await meeting_intelligence.generate_ai_notes(42) is True
