"""
OpenAI implementation of AIClient.
Uses chat.completions for images/text, Files+Responses API for PDF OCR.
Structured outputs for extraction.
"""
from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from clichefactory._engine.ai_clients.mime_support import is_default_direct_bytes_mime
from clichefactory._engine.ai_clients.prompts import DEFAULT_EXTRACTION_PROMPT, SIMPLE_OCR_PROMPT
from clichefactory._engine.ai_clients.protocol import UnsupportedBytesMimeError
from clichefactory._engine.ai_clients.json_utils import safe_json_loads
from clichefactory._engine.ai_clients.schema_compat import (
    OPENAI_UNSUPPORTED,
    has_keywords,
    move_to_description,
)
from clichefactory._extract_validation import validate_or_raise_raw

if TYPE_CHECKING:
    from clichefactory._engine.ai_clients.usage_tracker import UsageTracker

T = TypeVar("T", bound=BaseModel)


def _model_name_for_openai(name: str) -> str:
    """Strip 'openai/' prefix if present."""
    if name.startswith("openai/"):
        return name[len("openai/") :].strip()
    return name.strip()


# Reasoning models (o-series) reject the temperature parameter entirely.
_NO_TEMPERATURE_PREFIXES = ("o1", "o3", "o4")


def _response_format(schema: type[BaseModel]):
    """The ``response_format`` for structured extraction with *schema*.

    Normally the Pydantic class itself, as before. When the schema has string
    length bounds, which OpenAI's strict mode rejects, they are moved into the
    field descriptions and a JSON-schema ``response_format`` is sent instead;
    the reply is then validated against *schema*, which still enforces them.
    """
    from openai.lib._parsing._completions import type_to_response_format_param

    param = type_to_response_format_param(schema)
    strict_schema = param["json_schema"]["schema"]
    if not has_keywords(strict_schema, OPENAI_UNSUPPORTED):
        return schema
    return {
        **param,
        "json_schema": {
            **param["json_schema"],
            "schema": move_to_description(strict_schema, OPENAI_UNSUPPORTED),
        },
    }


def _model_supports_temperature(model_name: str) -> bool:
    return not any(
        model_name == p or model_name.startswith(p + "-")
        for p in _NO_TEMPERATURE_PREFIXES
    )


class OpenAIAIClient:
    """AIClient implementation using OpenAI API."""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        api_base: str | None = None,
        max_tokens: int = 10000,
        temperature: float = 0.1,
        max_retries: int = 8,
    ) -> None:
        self._model_name = _model_name_for_openai(model_name)
        self._full_model_name = (
            model_name if model_name.startswith("openai/") else f"openai/{model_name}"
        )
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._supports_temperature = _model_supports_temperature(self._model_name)
        client_kwargs = {"api_key": api_key, "max_retries": max_retries}
        if api_base:
            client_kwargs["base_url"] = api_base
        self._client = OpenAI(**client_kwargs)
        self._cost_tracker: UsageTracker | None = None

    def set_cost_tracker(self, cost_tracker: "UsageTracker | None") -> None:
        self._cost_tracker = cost_tracker

    def _chat_kwargs(self) -> dict:
        """Common kwargs for chat.completions calls. Uses max_completion_tokens
        (the unified successor to max_tokens) and skips temperature for reasoning
        models that reject it."""
        kwargs: dict = {"max_completion_tokens": self._max_tokens}
        if self._supports_temperature:
            kwargs["temperature"] = self._temperature
        return kwargs

    def _record_usage(self, usage) -> None:
        if not self._cost_tracker or not usage:
            return
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        self._cost_tracker.add_ocr_usage(self._full_model_name, pt, ct, 0)

    def ocr(self, content: bytes, mime: str, prompt: str) -> str:
        """OCR a single document (PDF or image). Returns markdown."""
        if mime == "application/pdf":
            return self._ocr_pdf(content, prompt)
        return self._ocr_image(content, mime, prompt)

    def _ocr_image(self, content: bytes, mime: str, prompt: str) -> str:
        b64 = base64.b64encode(content).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"
        response = self._client.chat.completions.create(
            model=self._model_name,
            **self._chat_kwargs(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )
        self._record_usage(getattr(response, "usage", None))
        return (response.choices[0].message.content or "").strip()

    def _ocr_pdf(self, content: bytes, prompt: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(content)
            path = f.name
        file = None
        try:
            with open(path, "rb") as fp:
                file = self._client.files.create(file=fp, purpose="assistants")
            response = self._client.responses.create(
                model=self._model_name,
                max_output_tokens=self._max_tokens,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_file", "file_id": file.id},
                            {"type": "input_text", "text": prompt},
                        ],
                    }
                ],
            )
            self._record_usage(getattr(response, "usage", None))
            return (response.output_text or "").strip()
        finally:
            if file is not None:
                try:
                    self._client.files.delete(file.id)
                except Exception:
                    pass
            Path(path).unlink(missing_ok=True)

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
        """OCR multiple images in one request."""
        content: list = [{"type": "text", "text": prompt}]
        for img_bytes in images:
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
        response = self._client.chat.completions.create(
            model=self._model_name,
            **self._chat_kwargs(),
            messages=[{"role": "user", "content": content}],
        )
        self._record_usage(getattr(response, "usage", None))
        return (response.choices[0].message.content or "").strip()

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
        return self._extract_chat(
            full_prompt, schema, raise_on_validation_error=raise_on_validation_error
        )

    def extract_json(self, text: str, prompt: str) -> dict:
        """Schema-less JSON extraction: prompt + document text -> plain dict."""
        full_prompt = f"{prompt}\n\nDocument:\n{text}"
        response = self._client.chat.completions.create(
            model=self._model_name,
            **self._chat_kwargs(),
            messages=[{"role": "user", "content": full_prompt}],
            response_format={"type": "json_object"},
        )
        self._record_usage(getattr(response, "usage", None))
        raw = response.choices[0].message.content or "{}"
        return safe_json_loads(
            raw,
            error_prefix="OpenAI returned invalid JSON for freeform extraction",
        )

    def supports_bytes(self, mime: str) -> bool:
        """OpenAI accepts PDFs (via Files+Responses) and common image MIMEs."""
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
        """End-to-end extraction. OpenAI runs OCR first then extracts.

        For non-image / non-PDF MIME types (EML, DOCX, XLSX, ODT, …),
        pre-flight with :meth:`supports_bytes` and parse to markdown
        first; calling this method with an unsupported *mime* raises
        :class:`UnsupportedBytesMimeError`.
        """
        if not self.supports_bytes(mime):
            raise UnsupportedBytesMimeError(mime, "OpenAI")
        markdown = self.ocr(content, mime, SIMPLE_OCR_PROMPT)
        return self.extract(
            markdown,
            schema,
            prompt,
            raise_on_validation_error=raise_on_validation_error,
        )

    def _extract_chat(
        self,
        prompt: str,
        schema: type[T],
        *,
        raise_on_validation_error: bool = True,
    ) -> T:
        """Call chat.completions with structured output."""
        response = self._client.beta.chat.completions.parse(
            model=self._model_name,
            **self._chat_kwargs(),
            messages=[{"role": "user", "content": prompt}],
            response_format=_response_format(schema),
        )
        self._record_usage(getattr(response, "usage", None))
        parsed = response.choices[0].message.parsed
        if parsed is not None:
            return parsed
        raw = response.choices[0].message.content or "{}"
        data = safe_json_loads(
            raw,
            error_prefix="OpenAI returned invalid JSON for schema extraction",
        )
        return validate_or_raise_raw(
            schema, data, raise_on_validation_error=raise_on_validation_error
        )
