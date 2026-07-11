"""Search routes — Phase 3 MVP.

PR7: user_id no longer defaults to 1.
Accept it from the authenticated user session (or a header).
Until a proper auth dependency is wired, the caller must pass
`X-User-Id` header or a `user_id` query parameter.
"""

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, Optional

from .database import get_db
from .search_service import search_meetings, ask_about_meetings

router = APIRouter()


def _resolve_user_id(
    x_user_id: Annotated[Optional[str], Header()] = None,
    user_id_param: Annotated[Optional[int], Query()] = None,
) -> int:
    """Resolve the authenticated user ID.

    Priority: X-User-Id header > user_id query parameter.
    Raises 401 if neither is supplied.

    TODO: replace with a real auth dependency once the gateway
    provides session claims.
    """
    if x_user_id is not None:
        try:
            return int(x_user_id)
        except ValueError:
            pass
    if user_id_param is not None:
        return user_id_param
    raise HTTPException(status_code=401, detail="Authentication required: provide X-User-Id header or user_id parameter")


@router.get("/internal/search")
async def search_endpoint(
    q: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(_resolve_user_id),
):
    return await search_meetings(q, user_id, db, limit)


@router.post("/internal/search/ask")
async def ask_endpoint(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(_resolve_user_id),
):
    question = data.get("question", "")
    if not question:
        raise HTTPException(status_code=400, detail="Missing 'question' field")
    return await ask_about_meetings(question, user_id, db)
