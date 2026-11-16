from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from knowflow.app.container import AppContainer
from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse
from knowflow.knowledge.conversations.schemas import (
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationPatchRequest,
    ConversationResponse,
)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


def container_for(request: Request) -> AppContainer:
    return request.app.state.container


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(request: Request, account: CurrentAccount) -> ConversationResponse:
    return await container_for(request).conversations.create(account.id)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(request: Request, account: CurrentAccount) -> ConversationListResponse:
    return await container_for(request).conversations.list_for_owner(account.id)


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: str, request: Request, account: CurrentAccount
) -> ConversationDetailResponse:
    return await container_for(request).conversations.get_detail(conversation_id, account.id)


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: str,
    payload: ConversationPatchRequest,
    request: Request,
    account: CurrentAccount,
) -> ConversationResponse:
    return await container_for(request).conversations.update(
        conversation_id,
        account.id,
        title=payload.title,
        knowledge_mode=payload.knowledge_mode,
        selected_source_ids=payload.selected_source_ids,
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str, request: Request, account: CurrentAccount
) -> None:
    await container_for(request).conversations.delete(conversation_id, account.id)
