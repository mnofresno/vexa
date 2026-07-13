from meeting_api.schemas import TranscriptionSegment


def test_transcription_segment_accepts_unknown_language():
    segment = TranscriptionSegment(
        start=0,
        end=1,
        text="Test",
        language="unknown",
    )

    assert segment.language == "unknown"
