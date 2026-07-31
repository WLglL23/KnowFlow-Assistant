"""Structured logging with conservative secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

request_id_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "knowflow_request_id", default=None
)

_SENSITIVE_KEY = re.compile(r"(?:password|secret|token|authorization|api[_-]?key)", re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|password|secret|token|api[_-]?key)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def _redact_string(value: str) -> str:
    value = _JWT.sub("[REDACTED_JWT]", value)
    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(item_key): _redact(item_value, str(item_key))
            for item_key, item_value in mapping.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [_redact(item) for item in sequence]
    return value


class SafeJsonFormatter(logging.Formatter):
    """Format records as JSON without emitting common credential fields."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        }
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_string(record.getMessage()),
            "request_id": getattr(record, "request_id", None) or request_id_context.get(),
        }
        if extras:
            payload["context"] = _redact(extras)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> logging.Logger:
    """Configure the application logger once and return it."""

    logger = logging.getLogger("knowflow")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(SafeJsonFormatter())
        logger.addHandler(handler)
    return logger
