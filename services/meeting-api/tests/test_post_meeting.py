from unittest.mock import AsyncMock, patch

import pytest

from meeting_api import post_meeting
from meeting_api.models import Meeting


class FakeSession:
    def __init__(self, meeting):
        self.meeting = meeting
        self.refresh_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _model, _meeting_id):
        return self.meeting

    async def refresh(self, meeting, attribute_names=None):
        self.refresh_calls.append(attribute_names)
        meeting.data = {
            **meeting.data,
            "ai_notes": {"summary": "Generated notes"},
            "ai_notes_model": "ollama/qwen3.5:9b",
        }

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_post_meeting_preserves_notes_saved_by_separate_session():
    meeting = Meeting(
        id=16,
        data={"post_meeting_state": "speakers_ready"},
    )
    db = FakeSession(meeting)

    with (
        patch.object(post_meeting, "async_session_local", return_value=db),
        patch(
            "meeting_api.diarization_service.run_diarization",
            new=AsyncMock(return_value={"total_speakers": 1}),
        ),
        patch(
            "meeting_api.meeting_intelligence.generate_ai_notes",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            post_meeting, "send_completion_webhook", new=AsyncMock(return_value=None)
        ),
        patch.object(
            post_meeting, "fire_post_meeting_hooks", new=AsyncMock(return_value=None)
        ),
    ):
        await post_meeting.run_all_tasks(16)

    assert db.refresh_calls == [["data"]]
    assert meeting.data["ai_notes"]["summary"] == "Generated notes"
    assert meeting.data["ai_notes_model"] == "ollama/qwen3.5:9b"
    assert meeting.data["post_meeting_state"] == "complete"
