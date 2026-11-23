"""
Anthropic implementation of AIClient.
Uses Messages API with image/PDF content blocks.
Structured outputs for extraction.
"""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING, TypeVar

from anthropic import Anthropic
from pydantic import BaseModel

from clichefactory._engine.ai_clients.mime_support import is_default_direct_bytes_mime
from clichefactory._engine.ai_clients.prompts import DEFAULT_EXTRACTION_PROMPT
from clichefactory._engine.ai_clients.protocol import UnsupportedBytesMimeError
from clichefactory._engine.ai_clients.json_utils import safe_json_loads
from clichefactory._engine.ai_clients.schema_compat import (
    ANTHROPIC_UNSUPPORTED,
    move_to_description,
)
from clichefactory._extract_validation import validate_or_raise_raw

if TYPE_CHECKING:
    from clichefactory._engine.ai_clients.usage_tracker import UsageTracker

T = TypeVar("T", bound=BaseModel)


def _model_name_for_anthropic(name: str) -> str:
    """Strip 'anthropic/' prefix if present."""
    if name.startswith("anthropic/"):
        return name[len("anthropic/") :].strip()
    return name.strip()


def _add_additional_properties_false(schema: dict) -> dict:
    """Recursively set additionalProperties=false on every object node.

    Anthropic's structured output API requires this to be explicitly present
    on all object types (root and nested $defs alike).
    """
    import copy

    schema = copy.deepcopy(schema)

    def _walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            node.setdefault("additionalProperties", False)
        for key in ("properties", "$defs", "definitions"):
            if key in node:
                for v in node[key].values():
                    _walk(v)
        for key in ("items", "additionalItems"):
            if key in node and isinstance(node[key], dict):
                _walk(node[key])
        for key in ("anyOf", "allOf", "oneOf"):
            if key in node:
                for v in node[key]:
                    _walk(v)

    _walk(schema)
    return schema


def _output_schema(schema: type[BaseModel]) -> dict:
    """The JSON Schema sent as Claude's output format.

    Value constraints Claude's structured outputs reject (string lengths, numeric
    bounds, patterns) are moved into the field descriptions; the response is
    still validated against *schema*, which enforces them.
    """
    return move_to_description(
        _add_additional_properties_false(schema.model_json_schema()),
        ANTHROPIC_UNSUPPORTED,
    )


def _anthropic_media_block(content: bytes, mime: str) -> dict:
    """Build a Messages API content block for bytes + MIME.

    PDFs must use ``type: document`` with ``application/pdf``; image blocks
    only allow image/jpeg, image/png, image/gif, image/webp (API returns 400 otherwise).
    """
    b64 = base64.b64encode(content).decode("utf-8")
    if mime == "application/pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": b64,
            },
        }
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": b64,
        },
    }


class AnthropicAIClient:
    """AIClient implementation using Anthropic Messages API."""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        max_tokens: int = 10000,
        temperature: float = 0.1,
        max_retries: int = 8,
    ) -> None:
        self._model_name = _model_name_for_anthropic(model_name)
        self._full_model_name = (
            model_name
            if model_name.startswith("anthropic/")
            else f"anthropic/{model_name}"
        )
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = Anthropic(api_key=api_key, max_retries=max_retries)
        self._cost_tracker: UsageTracker | None = None

    def set_cost_tracker(self, cost_tracker: "UsageTracker | None") -> None:
        self._cost_tracker = cost_tracker

    def _record_usage(self, usage) -> None:
        if not self._cost_tracker or not usage:
            return
        pt = getattr(usage, "input_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", 0) or 0
        self._cost_tracker.add_ocr_usage(self._full_model_name, pt, ct, 0)

    def _messages_create(self, **kwargs):
        """Run Messages API via streaming and return the final Message.

        The Anthropic Python SDK requires streaming when a request may exceed
        ~10 minutes (e.g. very large max_tokens). ``messages.stream`` + ``get_final_message``
        matches non-streaming behavior for callers.
        """
        with self._client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def _ocr_image(self, content: bytes, mime: str, prompt: str) -> str:
        response = self._messages_create(
            model=self._model_name,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        _anthropic_media_block(content, mime),
                    ],
                }
            ],
        )
        self._record_usage(getattr(response, "usage", None))
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        return text.strip()

    def ocr(self, content: bytes, mime: str, prompt: str) -> str:
        """OCR a single document (PDF or image). Anthropic supports PDF via base64."""
        return self._ocr_image(content, mime, prompt)

    def ocr_pages(
        self, content: bytes, page_numbers: list[int], prompt: str
    ) -> dict[int, str]:
        """OCR specific pages of a PDF. Extracts pages and calls ocr for each."""
        import fitz

        doc = fitz.open(stream=content, filetype="pdf")
        result: dict[int, str] = {}
        for page_no in page_numbers:
            idx = page_no - 1
            if 0 <= idx < len(doc):
                single = fitz.open()
                single.insert_pdf(doc, from_page=idx, to_page=idx)
                pdf_bytes = single.tobytes()
                single.close()
                try:
                    md = self.ocr(pdf_bytes, "application/pdf", prompt)
                    if md:
                        result[page_no] = md
                except Exception:
                    pass
        doc.close()
        return result

    def ocr_images(
        self, prompt: str, images: list[bytes], mime: str = "image/png"
    ) -> str:
        """OCR multiple images. Anthropic supports multiple image blocks in one message."""
        content: list = [{"type": "text", "text": prompt}]
        for img_bytes in images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(img_bytes).decode("utf-8"),
                    },
                }
            )
        response = self._messages_create(
            model=self._model_name,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[{"role": "user", "content": content}],
        )
        self._record_usage(getattr(response, "usage", None))
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        return text.strip()

    def ocr_batch(
        self, items: list[tuple[bytes, str]], prompt: str
    ) -> list[str]:
        """OCR a batch of items. Returns list of markdown strings."""
        results: list[str] = []
        for content_bytes, mime in items:
            try:
                md = self.ocr(content_bytes, mime, prompt)
                results.append(md)
            except Exception:
                results.append("")
        return results

    def extract(
        self,
        text: str,
        schema: type[T],
        prompt: str | None = None,
        *,
        raise_on_validation_error: bool = True,
    ) -> T:
        """Direct extraction: text + schema -> Pydantic instance."""
        instr = prompt or DEFAULT_EXTRACTION_PROMPT
        full_prompt = f"{instr}\n\nDocument:\n{text}"
        return self._extract_impl(
            full_prompt, schema, raise_on_validation_error=raise_on_validation_error
        )

    def extract_json(self, text: str, prompt: str) -> dict:
        """Schema-less JSON extraction: prompt + document text -> plain dict."""
        full_prompt = f"{prompt}\n\nDocument:\n{text}"
        response = self._messages_create(
            model=self._model_name,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[{"role": "user", "content": full_prompt}],
        )
        self._record_usage(getattr(response, "usage", None))
        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text
        return safe_json_loads(
            raw or "{}",
            error_prefix="Anthropic returned invalid JSON for freeform extraction",
        )

    def supports_bytes(self, mime: str) -> bool:
        """Anthropic accepts PDFs (document block) and common image MIMEs."""
        return is_default_direct_bytes_mime(mime)

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

        Anthropic supports PDFs (``document`` block) and the image MIMEs
        ``image/jpeg``, ``image/png``, ``image/gif``, ``image/webp``. For
        other MIME types pre-flight with :meth:`supports_bytes` and parse
        to markdown first; calling this method with an unsupported *mime*
        raises :class:`UnsupportedBytesMimeError`.
        """
        if not self.supports_bytes(mime):
            raise UnsupportedBytesMimeError(mime, "Anthropic")
        instr = prompt or DEFAULT_EXTRACTION_PROMPT
        content_blocks: list = [{"type": "text", "text": instr}]
        content_blocks.append(_anthropic_media_block(content, mime))
        return self._extract_impl_blocks(
            content_blocks,
            schema,
            raise_on_validation_error=raise_on_validation_error,
        )

    def _extract_impl(
        self,
        prompt: str,
        schema: type[T],
        *,
        raise_on_validation_error: bool = True,
    ) -> T:
        """Call messages.create with text and structured output."""
        response = self._messages_create(
            model=self._model_name,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": _output_schema(schema),
                }
            },
        )
        self._record_usage(getattr(response, "usage", None))
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        data = safe_json_loads(
            text or "{}",
            error_prefix="Anthropic returned invalid JSON for schema extraction",
        )
        return validate_or_raise_raw(
            schema, data, raise_on_validation_error=raise_on_validation_error
        )

    def _extract_impl_blocks(
        self,
        content_blocks: list,
        schema: type[T],
        *,
        raise_on_validation_error: bool = True,
    ) -> T:
        """Call messages.create with multimodal content and structured output."""
        response = self._messages_create(
            model=self._model_name,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[{"role": "user", "content": content_blocks}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": _output_schema(schema),
                }
            },
        )
        self._record_usage(getattr(response, "usage", None))
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        data = safe_json_loads(
            text or "{}",
            error_prefix="Anthropic returned invalid JSON for schema extraction",
        )
        return validate_or_raise_raw(
            schema, data, raise_on_validation_error=raise_on_validation_error
        )
