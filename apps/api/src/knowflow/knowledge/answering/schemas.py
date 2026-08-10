from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content: str
    client_request_id: str
