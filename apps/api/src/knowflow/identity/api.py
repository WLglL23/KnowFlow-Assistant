from __future__ import annotations

from fastapi import APIRouter, Request

from knowflow.app.container import AppContainer
from knowflow.app.errors import AppError
from knowflow.identity.schemas import AccountResponse, LoginRequest, LoginResponse, MeResponse

router = APIRouter(prefix="/api/auth", tags=["identity"])


def container_for(request: Request) -> AppContainer:
    return request.app.state.container


async def current_account(request: Request) -> AccountResponse:
    authorization = request.headers.get("Authorization")
    if authorization is None:
        raise AppError(401, "AUTH_REQUIRED", "需要认证")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AppError(401, "AUTH_INVALID", "认证失败")
    return await container_for(request).identity.account_from_token(parts[1])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request) -> LoginResponse:
    return await container_for(request).identity.login(payload.username, payload.password)


@router.post("/guest", response_model=LoginResponse)
async def guest_login(request: Request) -> LoginResponse:
    return await container_for(request).identity.create_guest()


@router.get("/me", response_model=MeResponse)
async def me(request: Request) -> MeResponse:
    return MeResponse(account=await current_account(request))
