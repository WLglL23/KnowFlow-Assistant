from pydantic import BaseModel, ConfigDict

from knowflow.identity.models import AccountRole


class LoginRequest(BaseModel):
    username: str
    password: str


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str
    username: str
    role: AccountRole
    is_guest: bool


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 28800
    account: AccountResponse


class MeResponse(BaseModel):
    account: AccountResponse
