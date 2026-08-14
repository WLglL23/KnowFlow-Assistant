from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse

router = APIRouter(prefix="/api/messages", tags=["citations"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


@router.get("/{message_id}/citations/{citation_index}/preview")
async def citation_preview(
    message_id: str,
    citation_index: int,
    request: Request,
    account: CurrentAccount,
) -> Response:
    content, filename = await request.app.state.container.citations.render(
        message_id, citation_index, account.id
    )
    safe_filename = filename.replace("\"", "")
    return Response(
        content=content,
        status_code=200,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )
