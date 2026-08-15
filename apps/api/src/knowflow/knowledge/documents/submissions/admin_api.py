from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response

from knowflow.app.errors import AppError
from knowflow.identity.api import current_account
from knowflow.identity.models import AccountRole
from knowflow.identity.schemas import AccountResponse
from knowflow.knowledge.documents.schemas import SubmissionListResponse, SubmissionResponse

router = APIRouter(prefix="/api/admin/knowledge/submissions", tags=["admin-submissions"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


def require_admin(account: AccountResponse) -> None:
    if account.role != AccountRole.ADMIN:
        raise AppError(403, "FORBIDDEN", "禁止访问")


@router.get("", response_model=SubmissionListResponse)
async def list_admin_submissions(
    request: Request, account: CurrentAccount
) -> SubmissionListResponse:
    require_admin(account)
    return await request.app.state.container.submissions.list_for_admin()


@router.get("/{submission_id}/document")
async def download_pending_submission(
    submission_id: str,
    request: Request,
    account: CurrentAccount,
) -> Response:
    require_admin(account)
    content, filename = await request.app.state.container.submissions.read_pending_document(
        submission_id
    )
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(
        content=content,
        status_code=200,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@router.post("/{submission_id}/reject", response_model=SubmissionResponse)
async def reject_submission(
    submission_id: str,
    request: Request,
    account: CurrentAccount,
) -> SubmissionResponse:
    require_admin(account)
    return await request.app.state.container.submissions.reject(submission_id, account.id)


@router.post("/{submission_id}/approve", response_model=SubmissionResponse)
async def approve_submission(
    submission_id: str,
    request: Request,
    account: CurrentAccount,
) -> SubmissionResponse:
    require_admin(account)
    return await request.app.state.container.publication.approve(submission_id, account.id)
