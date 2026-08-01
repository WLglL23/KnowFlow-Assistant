from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.app.errors import AppError
from knowflow.identity.models import Account, AccountRole
from knowflow.identity.schemas import AccountResponse, LoginResponse

JWT_ALGORITHM = "HS256"
JWT_EXPIRES_IN = 28800

_PASSWORD_HASH = PasswordHash.recommended()
_DUMMY_PASSWORD_HASH = _PASSWORD_HASH.hash("knowflow-dummy-password")


def auth_invalid() -> AppError:
    return AppError(401, "AUTH_INVALID", "认证失败")


class IdentityService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        jwt_secret: str,
    ) -> None:
        self._sessions = sessions
        self._jwt_secret = jwt_secret

    async def initialize_seed_accounts(self, settings: Settings) -> None:
        seeds = (
            (
                settings.seed_employee_username,
                settings.seed_employee_password.get_secret_value(),
                settings.seed_employee_display_name,
                AccountRole.EMPLOYEE,
            ),
            (
                settings.seed_admin_username,
                settings.seed_admin_password.get_secret_value(),
                settings.seed_admin_display_name,
                AccountRole.ADMIN,
            ),
        )
        async with self._sessions() as session, session.begin():
            for username, password, display_name, role in seeds:
                existing = await session.scalar(
                    select(Account).where(Account.username == username)
                )
                if existing is not None:
                    if (
                        existing.role != role
                        or existing.is_guest
                        or existing.password_hash is None
                    ):
                        raise RuntimeError("seed account role conflict")
                    continue
                session.add(
                    Account(
                        id=self._new_account_id(),
                        username=username,
                        password_hash=_PASSWORD_HASH.hash(password),
                        display_name=display_name,
                        role=role,
                        is_guest=False,
                    )
                )

    async def login(self, username: str, password: str) -> LoginResponse:
        async with self._sessions() as session:
            account = await session.scalar(select(Account).where(Account.username == username))

        password_hash = account.password_hash if account is not None else _DUMMY_PASSWORD_HASH
        password_matches = _PASSWORD_HASH.verify(password, password_hash or _DUMMY_PASSWORD_HASH)
        if account is None or account.password_hash is None or not password_matches:
            raise auth_invalid()
        return self._login_response(account)

    async def create_guest(self) -> LoginResponse:
        guest_suffix = uuid.uuid4().hex
        account = Account(
            id=self._new_account_id(),
            username=f"guest_{guest_suffix}",
            password_hash=None,
            display_name=f"Guest {guest_suffix[:8]}",
            role=AccountRole.EMPLOYEE,
            is_guest=True,
        )
        async with self._sessions() as session, session.begin():
            session.add(account)
        return self._login_response(account)

    async def account_from_token(self, token: str) -> AccountResponse:
        try:
            payload: dict[str, Any] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
                token,
                self._jwt_secret,
                algorithms=[JWT_ALGORITHM],
                options={"require": ["sub", "iat", "exp"]},
            )
        except jwt.InvalidTokenError as exc:
            raise auth_invalid() from exc

        account_id = payload.get("sub")
        if not isinstance(account_id, str) or not account_id:
            raise auth_invalid()
        async with self._sessions() as session:
            account = await session.get(Account, account_id)
        if account is None:
            raise auth_invalid()
        return AccountResponse.model_validate(account)

    def _login_response(self, account: Account) -> LoginResponse:
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            "sub": account.id,
            "iat": now,
            "exp": now + timedelta(seconds=JWT_EXPIRES_IN),
        }
        token = jwt.encode(  # pyright: ignore[reportUnknownMemberType]
            claims,
            self._jwt_secret,
            algorithm=JWT_ALGORITHM,
        )
        return LoginResponse(
            access_token=token,
            account=AccountResponse.model_validate(account),
        )

    @staticmethod
    def _new_account_id() -> str:
        return f"acc_{uuid.uuid4().hex}"
