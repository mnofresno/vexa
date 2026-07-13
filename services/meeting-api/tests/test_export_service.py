from unittest.mock import AsyncMock, MagicMock

import pytest

from meeting_api.export_service import export_meeting_markdown
from meeting_api.models import Meeting, Transcription


@pytest.mark.asyncio
async def test_export_includes_completed_recording_from_meeting_data():
    meeting = Meeting(
        id=16,
        platform="google_meet",
        platform_specific_id="abc-defg-hij",
        status="completed",
        data={
            "recordings": [
                {
                    "status": "completed",
                    "source": "bot",
                    "session_uid": "session-16",
                    "created_at": "2026-07-12T16:52:39",
                    "completed_at": "2026-07-12T16:57:38",
                }
            ]
        },
    )
    segment = Transcription(
        meeting_id=16,
        start_time=0,
        end_time=10,
        text="Transcript text",
        speaker="Mariano",
        status="final",
    )

    segment_result = MagicMock()
    segment_result.scalars.return_value.all.return_value = [segment]
    recording_result = MagicMock()
    recording_result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.get = AsyncMock(return_value=meeting)
    db.execute = AsyncMock(side_effect=[segment_result, recording_result])

    markdown = await export_meeting_markdown(16, db)

    assert "## Recording" in markdown
    assert r"session\-16" in markdown
    assert "## Transcript" in markdown
