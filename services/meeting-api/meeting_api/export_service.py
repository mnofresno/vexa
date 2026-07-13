"""Markdown export service — generate .md from meeting record.

PR7 changes:
    - Export only final ordered segments.
- Include recording metadata, speakers, notes, decisions and action items.
- Escape untrusted Markdown content to prevent injection.
- Add provenance metadata: meeting ID, generated timestamp and component versions.
"""

import datetime
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Meeting, Transcription, Recording

logger = logging.getLogger("meeting_api.export")

VEXA_VERSION = "0.3.0-pr7"


def _md_escape(text: str) -> str:
    """Escape Markdown-active characters in untrusted text.

    Prevents injection of headers, links, images, code-fences, emphasis, etc.
    Preserves newlines so paragraphs still render naturally.
    """
    if not text:
        return ""
    s = str(text)
    s = s.replace("\\", "\\\\")
    s = s.replace("`", "\\`")
    s = s.replace("$", "\\$")
    s = s.replace("&", "&amp;")
    s = s.replace("<", "&lt;")
    s = s.replace(">", "&gt;")
    s = s.replace("#", "\\#")
    s = s.replace("*", "\\*")
    s = s.replace("_", "\\_")
    s = s.replace("-", "\\-")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    s = s.replace("(", "\\(")
    s = s.replace(")", "\\)")
    s = s.replace("!", "\\!")
    s = s.replace("{", "\\{")
    s = s.replace("}", "\\}")
    return s


async def export_meeting_markdown(meeting_id: int, db: AsyncSession) -> Optional[str]:
    """Generate a Markdown document for a meeting.

    Only exports final segments. Includes recording
    metadata, speaker summary, AI notes (decisions, action items, summary,
    unresolved questions), and provenance footer.

    Returns the .md text or None if meeting not found.
    """
    meeting = await db.get(Meeting, meeting_id)
    if not meeting:
        return None

    # Only finalized, ordered segments
    result = await db.execute(
        select(Transcription)
        .where(
            Transcription.meeting_id == meeting_id,
            Transcription.status == "final",
        )
        .order_by(Transcription.start_time)
    )
    segments = result.scalars().all()

    # Recording metadata
    rec_result = await db.execute(
        select(Recording).where(
            Recording.meeting_id == meeting_id,
            Recording.status == "completed",
        )
    )
    recordings = list(rec_result.scalars().all())
    recording_sessions = {rec.session_uid for rec in recordings if rec.session_uid}
    for recording in (meeting.data or {}).get("recordings", []):
        if not isinstance(recording, dict) or recording.get("status") != "completed":
            continue
        session_uid = recording.get("session_uid")
        if session_uid and session_uid in recording_sessions:
            continue
        recordings.append(recording)

    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []

    # Header
    lines.append(f"# Meeting: {_md_escape(meeting.platform)} — {_md_escape(meeting.platform_specific_id or 'unknown')}")
    lines.append("")
    lines.append(f"- **Platform:** {_md_escape(meeting.platform)}")
    lines.append(f"- **Meeting ID:** {meeting.id}")
    if meeting.start_time:
        lines.append(f"- **Start:** {meeting.start_time.isoformat()}")
    if meeting.end_time:
        lines.append(f"- **End:** {meeting.end_time.isoformat()}")
    lines.append(f"- **Status:** {_md_escape(meeting.status)}")
    lines.append("")

    # Recording metadata
    if recordings:
        lines.append("## Recording")
        lines.append("")
        for rec in recordings:
            value = rec.get if isinstance(rec, dict) else lambda key: getattr(rec, key, None)
            source = value("source")
            created_at = value("created_at")
            completed_at = value("completed_at")
            session_uid = value("session_uid")
            error_message = value("error_message")

            lines.append(f"- **Source:** {_md_escape(source or 'unknown')}")
            if created_at:
                captured = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
                lines.append(f"- **Captured:** {_md_escape(captured)}")
            if completed_at:
                completed = completed_at.isoformat() if hasattr(completed_at, "isoformat") else str(completed_at)
                lines.append(f"- **Completed:** {_md_escape(completed)}")
            if session_uid:
                lines.append(f"- **Session:** {_md_escape(session_uid)}")
            if error_message:
                lines.append(f"- **Error:** {_md_escape(error_message)}")
            lines.append("")

    # Transcript — finalized segments only
    if segments:
        lines.append("## Transcript")
        lines.append("")
        for seg in segments:
            ts = format_timestamp(seg.start_time)
            speaker = _md_escape(seg.speaker or "Unknown")
            lines.append(f"**{ts} [{speaker}]** {_md_escape(seg.text)}")
            lines.append("")

        # Speaker summary
        speakers: dict[str, int] = {}
        for seg in segments:
            s = _md_escape(seg.speaker or "Unknown")
            speakers[s] = speakers.get(s, 0) + 1
        if len(speakers) > 1:
            lines.append("## Speakers")
            lines.append("")
            for name, count in sorted(speakers.items(), key=lambda x: -x[1]):
                lines.append(f"- **{name}**: {count} segments")
            lines.append("")
    else:
        lines.append("*No finalized transcript available.*")
        lines.append("")

    # AI notes — summary, key moments, decisions, action items, unresolved
    data = meeting.data or {}
    ai_notes = data.get("ai_notes")
    if ai_notes and isinstance(ai_notes, dict):
        lines.append("## Notes")
        lines.append("")
        if ai_notes.get("summary"):
            lines.append(f"**Summary:** {_md_escape(ai_notes['summary'])}")
            lines.append("")
        if ai_notes.get("key_moments"):
            lines.append("**Key Moments:**")
            for km in ai_notes["key_moments"]:
                lines.append(f"- {_md_escape(str(km))}")
            lines.append("")
        if ai_notes.get("decisions"):
            lines.append("**Decisions:**")
            for d in ai_notes["decisions"]:
                lines.append(f"- {_md_escape(str(d))}")
            lines.append("")
        if ai_notes.get("action_items"):
            lines.append("**Action Items:**")
            for item in ai_notes["action_items"]:
                if isinstance(item, dict):
                    desc = _md_escape(item.get("description", ""))
                    assignee = item.get("assignee", "")
                    lines.append(
                        f"- [ ] {desc}"
                        + (f" ({_md_escape(str(assignee))})" if assignee else "")
                    )
                else:
                    lines.append(f"- [ ] {_md_escape(str(item))}")
            lines.append("")
        if ai_notes.get("unresolved"):
            lines.append("**Unresolved Questions:**")
            for q in ai_notes["unresolved"]:
                lines.append(f"- {_md_escape(str(q))}")
            lines.append("")

    # Provenance metadata
    lines.append("---")
    lines.append(f"*Generated: {now} | Vexa export component v{VEXA_VERSION} | Meeting {meeting_id}*")

    return "\n".join(lines)


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
