"""AIClient protocol: unified interface for OCR and extraction across providers."""
from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class UnsupportedBytesMimeError(ValueError):
    """Raised when ``extract_from_bytes`` is called with a MIME the vendor
    does not accept as raw bytes (e.g. ``message/rfc822`` for any of the
    mainstream hosted LLMs).

    Callers should pre-flight via
    :func:`clichefactory._engine.ai_clients.mime_support.client_supports_bytes`
    and route unsupported MIMEs through the parser pipeline (markdown)
    instead.
    """

    def __init__(self, mime: str | None, vendor: str) -> None:
        self.mime = mime or ""
        self.vendor = vendor
        super().__init__(
            f"{vendor} does not accept {self.mime!r} as raw bytes; "
            "parse to markdown first or call extract(text, schema)."
        )


class AIClient(Protocol):
    """Unified LLM client for OCR, extraction, and optional end-to-end extraction."""

    def ocr(self, content: bytes, mime: str, prompt: str) -> str:
        """OCR a single document (PDF or image). Returns markdown."""
        ...

    def ocr_pages(
        self, content: bytes, page_numbers: list[int], prompt: str
    ) -> dict[int, str]:
        """OCR specific pages of a PDF. Returns {page_no: markdown}."""
        ...

    def ocr_images(
        self, prompt: str, images: list[bytes], mime: str = "image/png"
    ) -> str:
        """OCR multiple images in one request. Returns combined markdown."""
        ...

    def ocr_batch(
        self, items: list[tuple[bytes, str]], prompt: str
    ) -> list[str]:
        """OCR a batch of (content_bytes, mime) items. Returns list of markdown strings."""
        ...

    def extract(
        self,
        text: str,
        schema: type[T],
        prompt: str | None = None,
        *,
        raise_on_validation_error: bool = True,
    ) -> T:
        """Direct extraction: text + schema -> Pydantic instance. No DSPy."""
        ...

    def extract_json(
        self,
        text: str,
        prompt: str,
    ) -> dict:
        """Schema-less JSON extraction: text + prompt -> plain dict.

        Unlike ``extract``, this never sends a ``response_schema`` to the
        provider.  The LLM is asked to return ``application/json`` and the
        response is parsed into a Python dict.  Use this for freeform /
        suggest-model flows where no Pydantic model is available.
        """
        ...

    def extract_from_bytes(
        self,
        content: bytes,
        mime: str,
        schema: type[T],
        prompt: str | None = None,
        *,
        raise_on_validation_error: bool = True,
    ) -> T:
        """End-to-end extraction: bytes + schema -> Pydantic instance.

        Raises :class:`UnsupportedBytesMimeError` when the vendor does not
        accept *mime* as raw bytes. Pre-flight with :meth:`supports_bytes`
        and route unsupported MIMEs through the parser pipeline.
        """
        ...

    def supports_bytes(self, mime: str) -> bool:
        """Return ``True`` when ``extract_from_bytes`` can be called with *mime*.

        Used by callers (the SDK ``Cliche.extract`` fast path, the
        ClicheFactory extraction service) to decide between sending raw
        bytes to the vendor and parsing to markdown locally first.
        """
        ...
