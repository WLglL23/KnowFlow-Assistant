"""FastAPI composition root for M01."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException

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
from knowflow.knowledge.conversations.api import router as conversations_router
from knowflow.knowledge.conversations.service import ConversationService


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = f"req_{uuid.uuid4().hex}"
    request.state.request_id = request_id
    token = request_id_context.set(request_id)
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            response = await unhandled_exception_handler(request, exc)
        response.headers["X-Request-ID"] = request_id
        return response
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
    container = AppContainer(
        settings=resolved_settings,
        engine=engine,
        sessions=sessions,
        identity=identity,
        conversations=conversations,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        del app
        try:
            await identity.initialize_seed_accounts(resolved_settings)
            yield
        finally:
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
    app.middleware("http")(request_id_middleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(identity_router)
    app.include_router(conversations_router)
    return app
