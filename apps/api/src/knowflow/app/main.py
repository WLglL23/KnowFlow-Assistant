"""FastAPI composition root for M01."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from knowflow.app.config import Settings
from knowflow.app.container import AppContainer
from knowflow.app.errors import (
    AppError,
    app_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from knowflow.app.logging import configure_logging, request_id_context
from knowflow.identity.api import router as identity_router
from knowflow.identity.service import IdentityService
from knowflow.knowledge.answering.api import router as answering_router
from knowflow.knowledge.answering.chat import OpenAICompatibleChatAdapter
from knowflow.knowledge.answering.policy import core_experimental_policy
from knowflow.knowledge.answering.retrieval import AttachmentRetrieval
from knowflow.knowledge.answering.service import AnsweringService
from knowflow.knowledge.conversations.api import router as conversations_router
from knowflow.knowledge.conversations.service import ConversationService
from knowflow.knowledge.documents.api import router as documents_router
from knowflow.knowledge.documents.attachments import AttachmentDocuments
from knowflow.knowledge.documents.citations import CitationPreview
from knowflow.knowledge.documents.citations.api import router as citation_router
from knowflow.knowledge.documents.cleanup import CleanupDocuments
from knowflow.knowledge.documents.cleanup.api import router as cleanup_router
from knowflow.knowledge.documents.object_store import PrivateObjectStore
from knowflow.knowledge.documents.pdf import PdfDocuments
from knowflow.knowledge.documents.publication import PublicationDocuments
from knowflow.knowledge.documents.publication.api import router as publication_router
from knowflow.knowledge.documents.publication.user_api import router as user_publication_router
from knowflow.knowledge.documents.submissions import SubmissionDocuments
from knowflow.knowledge.documents.submissions.admin_api import router as admin_submissions_router
from knowflow.knowledge.documents.submissions.api import router as submissions_router


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = f"req_{uuid.uuid4().hex}"
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        token = request_id_context.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [item for item in headers if item[0].lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await self._app(scope, receive, send_with_request_id)
            except Exception as exc:
                response = await unhandled_exception_handler(Request(scope), exc)
                await response(scope, receive, send_with_request_id)
        finally:
            request_id_context.reset(token)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the M01 app; external connections are deferred to its lifespan."""

    if settings is None:
        resolved_settings = Settings()  # pyright: ignore[reportCallIssue]
    else:
        resolved_settings = settings
    engine = create_async_engine(resolved_settings.mysql_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = IdentityService(
        sessions,
        resolved_settings.jwt_secret.get_secret_value(),
    )
    conversations = ConversationService(sessions)
    chat = OpenAICompatibleChatAdapter(
        base_url=resolved_settings.chat_base_url,
        api_key=resolved_settings.chat_api_key,
        model=resolved_settings.chat_model,
    )
    retrieval = AttachmentRetrieval(
        elasticsearch_url=resolved_settings.elasticsearch_url,
        elasticsearch_index=resolved_settings.elasticsearch_index,
        embedding_base_url=resolved_settings.embedding_base_url,
        embedding_api_key=resolved_settings.embedding_api_key,
        embedding_model=resolved_settings.embedding_model,
        embedding_dimensions=resolved_settings.embedding_dimensions,
        rerank_base_url=resolved_settings.rerank_base_url,
        rerank_api_key=resolved_settings.rerank_api_key,
        rerank_model=resolved_settings.rerank_model,
    )
    pdf = PdfDocuments(resolved_settings)
    objects = PrivateObjectStore(resolved_settings)
    documents = AttachmentDocuments(sessions, resolved_settings, pdf, objects)
    submissions = SubmissionDocuments(sessions, resolved_settings, pdf, objects)
    publication = PublicationDocuments(sessions, resolved_settings, pdf, objects)
    cleanup = CleanupDocuments(sessions, resolved_settings, objects)
    answering = AnsweringService(
        sessions,
        chat,
        core_experimental_policy(resolved_settings.run_deadline_seconds),
        retrieval,
        cleanup=cleanup,
    )
    citations = CitationPreview(sessions, documents)
    container = AppContainer(
        settings=resolved_settings,
        engine=engine,
        sessions=sessions,
        identity=identity,
        conversations=conversations,
        answering=answering,
        retrieval=retrieval,
        documents=documents,
        submissions=submissions,
        publication=publication,
        citations=citations,
        cleanup=cleanup,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        del app
        try:
            await identity.initialize_seed_accounts(resolved_settings)
            await answering.recover_interrupted_runs()
            await cleanup.recover_startup()
            await documents.start()
            yield
        finally:
            await publication.stop()
            await documents.stop()
            await engine.dispose()

    configure_logging()
    app = FastAPI(
        title="KnowFlow API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.container = container
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(identity_router)
    app.include_router(conversations_router)
    app.include_router(answering_router)
    app.include_router(documents_router)
    app.include_router(citation_router)
    app.include_router(submissions_router)
    app.include_router(admin_submissions_router)
    app.include_router(publication_router)
    app.include_router(user_publication_router)
    app.include_router(cleanup_router)
    return app
