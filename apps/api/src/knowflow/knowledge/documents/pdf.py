from __future__ import annotations

import asyncio
import io
import os
import threading
from typing import Any, cast

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.io import DocumentStream

from knowflow.app.config import Settings
from knowflow.app.errors import AppError


class PdfDocuments:
    """Shared PDF preflight and conversion boundary for document capabilities."""

    def __init__(self, settings: Settings) -> None:
        self._max_pages = settings.document_max_pages
        self._converter_lock = threading.Lock()
        if settings.docling_hf_home:
            os.environ.setdefault("HF_HOME", settings.docling_hf_home)
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
        cast(Any, options.layout_options).engine_options.compile_model = False
        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )

    async def validate(
        self,
        filename: str,
        content_type: str,
        content: bytes,
        *,
        max_bytes: int,
    ) -> int:
        if len(content) > max_bytes:
            raise AppError(
                413,
                "DOCUMENT_TOO_LARGE",
                "文档超过限制",
                {"reason": "BYTES_EXCEEDED"},
            )
        if content_type != "application/pdf":
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": "MIME_MISMATCH"})
        if not filename.lower().endswith(".pdf"):
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": "EXTENSION_INVALID"})
        if not content.startswith(b"%PDF-"):
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": "SIGNATURE_INVALID"})
        if b"/Encrypt" in content:
            raise AppError(
                422,
                "INVALID_PDF",
                "PDF 无效",
                {"reason": "PASSWORD_PROTECTED"},
            )
        try:
            document = await asyncio.to_thread(self.convert, filename, content)
            pages = len(document.pages)
        except AppError:
            raise
        except Exception as exc:
            reason = "PAGE_COUNT_UNREADABLE" if b"/Type /Catalog" in content else "CORRUPT"
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": reason}) from exc
        if pages == 0:
            raise AppError(
                422,
                "INVALID_PDF",
                "PDF 无效",
                {"reason": "PAGE_COUNT_UNREADABLE"},
            )
        if pages > self._max_pages:
            raise AppError(
                413,
                "DOCUMENT_TOO_LARGE",
                "文档超过限制",
                {"reason": "PAGES_EXCEEDED"},
            )
        return pages

    def convert(self, filename: str, content: bytes) -> Any:
        # Docling's converter owns model state; concurrent users must serialize access.
        with self._converter_lock:
            result = self._converter.convert(
                DocumentStream(name=filename, stream=io.BytesIO(content))
            )
        return result.document
