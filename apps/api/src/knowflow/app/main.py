"""FastAPI composition root for M01."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from knowflow.app.config import Settings
from knowflow.app.container import AppContainer
from knowflow.app.errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from knowflow.app.logging import configure_logging, request_id_context


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = f"req_{uuid.uuid4().hex}"
    request.state.request_id = request_id
    token = request_id_context.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_context.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the M01 app without connecting to external services."""

    if settings is None:
        resolved_settings = Settings()  # pyright: ignore[reportCallIssue]
    else:
        resolved_settings = settings
    configure_logging()
    app = FastAPI(
        title="KnowFlow API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.container = AppContainer(settings=resolved_settings)
    app.middleware("http")(request_id_middleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    return app
