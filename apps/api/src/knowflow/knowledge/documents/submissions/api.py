from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile

from knowflow.app.errors import AppError
from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse
from knowflow.knowledge.documents.schemas import SubmissionListResponse

router = APIRouter(prefix="/api/knowledge/submissions", tags=["knowledge-submissions"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


@router.post("")
async def create_submission(
    request: Request,
    account: CurrentAccount,
    file: list[UploadFile] | None = File(default=None),
) -> Response:
    if file is None or len(file) != 1:
        raise AppError(422, "VALIDATION_FAILED", "请求校验失败")
    upload = file[0]
    content = await upload.read()
    status_code, payload = await request.app.state.container.submissions.submit(
        account.id,
        upload.filename or "",
        upload.content_type or "",
        content,
    )
    return Response(
        content=payload.model_dump_json(
            exclude={"submission"} if payload.submission is None else None
        ),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("/mine", response_model=SubmissionListResponse)
async def list_my_submissions(request: Request, account: CurrentAccount) -> SubmissionListResponse:
    return await request.app.state.container.submissions.list_for_owner(account.id)


@router.delete("/{submission_id}", status_code=204)
async def delete_submission(
    submission_id: str, request: Request, account: CurrentAccount
) -> None:
    await request.app.state.container.submissions.delete(submission_id, account.id)
