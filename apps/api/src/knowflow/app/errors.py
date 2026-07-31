"""Safe HTTP error envelope and exception handlers."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    413: "DOCUMENT_TOO_LARGE",
    422: "VALIDATION_FAILED",
    429: "PUBLICATION_CAPACITY_EXCEEDED",
}

_STATUS_MESSAGES = {
    400: "请求无效",
    401: "需要认证",
    403: "禁止访问",
    404: "资源不存在",
    409: "当前状态不允许此操作",
    413: "请求内容过大",
    422: "请求参数校验失败",
    429: "当前请求过于频繁",
}


def request_id_for(request: Request) -> str:
    return getattr(request.state, "request_id", "req_unknown")


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "request_id": request_id_for(request)},
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    status_code = getattr(exc, "status_code", 500)
    return error_response(
        request,
        status_code,
        _STATUS_CODES.get(status_code, "INTERNAL_ERROR"),
        _STATUS_MESSAGES.get(status_code, "服务暂时不可用"),
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return error_response(request, 422, "VALIDATION_FAILED", _STATUS_MESSAGES[422])


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger = logging.getLogger("knowflow.errors")
    logger.error(
        "Unhandled application exception",
        extra={"exception_type": type(exc).__name__},
    )
    return error_response(request, 500, "INTERNAL_ERROR", "服务暂时不可用")
