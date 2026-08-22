from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response

from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse
from knowflow.knowledge.documents.schemas import (
    KnowledgeSourceListResponse,
    KnowledgeSourceResponse,
    KnowledgeSourceUpdateRequest,
)
from knowflow.knowledge.documents.submissions.admin_api import require_admin

router = APIRouter(prefix="/api/admin/knowledge/sources", tags=["admin-sources"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


@router.get("", response_model=KnowledgeSourceListResponse)
async def list_sources(request: Request, account: CurrentAccount) -> KnowledgeSourceListResponse:
    require_admin(account)
    return await request.app.state.container.publication.list_sources()


@router.get("/{source_id}/document")
async def download_source_document(
    source_id: str,
    request: Request,
    account: CurrentAccount,
) -> Response:
    require_admin(account)
    content, filename = await request.app.state.container.publication.read_source_document(
        source_id
    )
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(
        content=content,
        status_code=200,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@router.patch("/{source_id}", response_model=KnowledgeSourceResponse)
async def update_source(
    source_id: str,
    payload: KnowledgeSourceUpdateRequest,
    request: Request,
    account: CurrentAccount,
) -> KnowledgeSourceResponse:
    require_admin(account)
    return await request.app.state.container.publication.set_enabled(source_id, payload.enabled)


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, request: Request, account: CurrentAccount) -> None:
    require_admin(account)
    await request.app.state.container.publication.delete_source(source_id)
