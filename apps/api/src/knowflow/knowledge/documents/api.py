from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile

from knowflow.app.errors import AppError
from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse
from knowflow.knowledge.documents.schemas import AttachmentListResponse, AttachmentResponse

router = APIRouter(prefix="/api/conversations/{conversation_id}/attachments", tags=["attachments"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


@router.post("")
async def upload_attachment(
    conversation_id: str,
    request: Request,
    account: CurrentAccount,
    file: list[UploadFile] | None = File(default=None),
) -> Response:
    if file is None or len(file) != 1:
        raise AppError(422, "VALIDATION_FAILED", "请求校验失败")
    upload = file[0]
    content = await upload.read()
    status, payload = await request.app.state.container.documents.upload(
        conversation_id,
        account.id,
        upload.filename or "",
        upload.content_type or "",
        content,
    )
    if payload.result in {"CREATED", "RETRY_QUEUED"}:
        asyncio.get_running_loop().call_soon(
            asyncio.create_task, request.app.state.container.documents.dispatch()
        )
    return Response(
        content=payload.model_dump_json(), status_code=status, media_type="application/json"
    )


@router.get("", response_model=AttachmentListResponse)
async def list_attachments(
    conversation_id: str, request: Request, account: CurrentAccount
) -> AttachmentListResponse:
    return await request.app.state.container.documents.list_for_owner(conversation_id, account.id)


@router.get("/{attachment_id}", response_model=AttachmentResponse)
async def attachment_detail(
    conversation_id: str, attachment_id: str, request: Request, account: CurrentAccount
) -> AttachmentResponse:
    return await request.app.state.container.documents.detail(
        conversation_id, attachment_id, account.id
    )


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(
    conversation_id: str, attachment_id: str, request: Request, account: CurrentAccount
) -> None:
    await request.app.state.container.documents.delete(
        conversation_id, attachment_id, account.id
    )
