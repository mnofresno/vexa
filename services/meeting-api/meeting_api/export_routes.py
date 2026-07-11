"""Meeting export routes — Markdown export endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .export_service import export_meeting_markdown

router = APIRouter()


@router.get("/internal/meetings/{meeting_id}/export")
async def export_meeting(
    meeting_id: int,
    format: str = "md",
    db: AsyncSession = Depends(get_db),
):
    if format != "md":
        raise HTTPException(status_code=400, detail="Only 'md' format supported")

    md = await export_meeting_markdown(meeting_id, db)
    if md is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")

    return Response(
        content=md,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f"attachment; filename=meeting-{meeting_id}.md",
        },
    )
