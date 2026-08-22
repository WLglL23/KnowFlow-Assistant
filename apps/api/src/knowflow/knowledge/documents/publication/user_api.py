from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from knowflow.app.errors import AppError
from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse
from knowflow.knowledge.documents.schemas import KnowledgeSourceListResponse

router = APIRouter(prefix="/api/knowledge/sources", tags=["knowledge-sources"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


@router.get("", response_model=KnowledgeSourceListResponse)
async def list_available_sources(
    request: Request, account: CurrentAccount
) -> KnowledgeSourceListResponse:
    if account.is_guest:
        raise AppError(403, "FORBIDDEN", "当前身份无权执行此操作")
    return await request.app.state.container.publication.list_available_sources()
