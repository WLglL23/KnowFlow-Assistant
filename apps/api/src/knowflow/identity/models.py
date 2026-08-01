from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Enum, String, text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AccountRole(StrEnum):
    ADMIN = "ADMIN"
    EMPLOYEE = "EMPLOYEE"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[AccountRole] = mapped_column(
        Enum(AccountRole, name="account_role", native_enum=True)
    )
    is_guest: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)")
    )
