from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from knowflow.app.errors import AppError
from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse

router = APIRouter(prefix="/api/admin/cleanup-failures", tags=["admin-cleanup"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


def require_admin(account: AccountResponse) -> None:
    if account.role.value != "ADMIN":
        raise AppError(403, "FORBIDDEN", "禁止访问")


@router.get("")
async def list_cleanup_failures(
    request: Request, account: CurrentAccount
) -> dict[str, list[dict[str, object]]]:
    require_admin(account)
    return await request.app.state.container.cleanup.list_failures()


@router.post("/{resource_type}/{resource_id}/retry")
async def retry_cleanup(
    resource_type: str,
    resource_id: str,
    request: Request,
    account: CurrentAccount,
) -> dict[str, object]:
    require_admin(account)
    return await request.app.state.container.cleanup.retry(resource_type, resource_id)
