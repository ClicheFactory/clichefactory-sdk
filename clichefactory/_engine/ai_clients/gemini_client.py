"""
Gemini implementation of AIClient.
OCR + extraction via Google GenAI.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, TypeVar

import httpx
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from pydantic import BaseModel

from clichefactory._engine.ai_clients.mime_support import is_default_direct_bytes_mime
from clichefactory._engine.ai_clients.prompts import DEFAULT_EXTRACTION_PROMPT
from clichefactory._engine.ai_clients.protocol import UnsupportedBytesMimeError
from clichefactory._engine.ai_clients.json_utils import safe_json_loads
from clichefactory._extract_validation import validate_or_raise_raw

if TYPE_CHECKING:
    from clichefactory._engine.ai_clients.usage_tracker import UsageTracker

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _escape_raw_newlines_in_json_strings(text: str) -> str:
    """
    Repair a common "almost JSON" failure:
    Gemini sometimes emits actual newlines inside JSON string literals, which
    makes the JSON invalid (JSON strings may not contain unescaped newlines).
    """

    out: list[str] = []
    in_string = False
    escape = False

    for ch in text:
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            continue

        # In a JSON string literal.
        if escape:
            out.append(ch)
            escape = False
            continue

        if ch == "\\":
            out.append(ch)
            escape = True
            continue

        if ch == '"':
            in_string = False
            out.append(ch)
            continue

        # Escape raw control characters that would otherwise break json.loads.
        if ch == "\n":
            out.append("\\n")
            continue
        if ch == "\r":
            out.append("\\r")
            continue
        if ch == "\t":
            out.append("\\t")
            continue

        out.append(ch)

    return "".join(out)


def _extract_json_substring(text: str) -> str | None:
    """Best-effort extraction of a JSON object from surrounding text/code fences."""
    if not text:
        return None

    s = text.strip()
    # Remove common markdown fences if present.
    s = re.sub(
        r"^```(?:json)?\s*|```$",
        "",
        s,
        flags=re.IGNORECASE | re.MULTILINE,
    ).strip()

    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end >= start:
        return s[start : end + 1]

    return None


def _safe_json_loads(text: str) -> dict:
    """
    Parse JSON with small, targeted repairs.
    Raises json.JSONDecodeError if it still can't parse.
    """
    raw = (text or "").strip()
    if raw == "":
        return {}

    # 1) Normal parse.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2) If the model returned JSON wrapped with extra text, extract the object.
    candidate = _extract_json_substring(raw)
    if candidate is not None:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # 3) Repair raw newlines inside JSON strings (another common failure mode).
        repaired = _escape_raw_newlines_in_json_strings(candidate)
        return json.loads(repaired)

    # 4) Final attempt: repair raw newlines and parse again.
    repaired = _escape_raw_newlines_in_json_strings(raw)
    return json.loads(repaired)


def _model_name_for_genai(name: str) -> str:
    """Strip 'gemini/' prefix if present; GenAI client expects e.g. 'gemini-3-flash-preview'."""
    if name.startswith("gemini/"):
        return name[len("gemini/") :].strip()
    return name.strip()


# Pre-Gemini-3 families: gemini-1.x, gemini-2.x, gemini-pro, gemini-flash, gemini-exp.
# The rolling ``gemini-{flash,flash-lite,pro}-latest`` aliases point at current models.
_PRE_GEMINI_3_RE = re.compile(
    r"^gemini-(?:[12](?:\.\d+)?|exp|(?:pro|flash)(?!-(?:lite-)?latest$))(?:-|$)"
)


def is_gemini_3_or_newer(model_name: str) -> bool:
    """True for Gemini 3+ models, which ignore (and will soon reject) sampling params.

    Google has deprecated ``temperature`` / ``top_p`` / ``top_k`` for Gemini 3+
    and ``thinking_budget`` in favour of ``thinking_level``; upcoming models
    return 400 INVALID_ARGUMENT when the deprecated fields are sent.
    """
    name = _model_name_for_genai(model_name).lower()
    if not name.startswith("gemini-"):
        return False
    return _PRE_GEMINI_3_RE.match(name) is None


def _extract_pdf_page_bytes(
    content: bytes, page_indices: list[int]
) -> list[tuple[int, bytes]]:
    """Extract specific pages from a PDF as separate single-page PDF bytes."""
    import fitz

    doc = fitz.open(stream=content, filetype="pdf")
    result: list[tuple[int, bytes]] = []
    for page_no in page_indices:
        idx = page_no - 1
        if 0 <= idx < len(doc):
            single = fitz.open()
            single.insert_pdf(doc, from_page=idx, to_page=idx)
            result.append((page_no, single.tobytes()))
            single.close()
    doc.close()
    return result


class GeminiAIClient:
    """AIClient implementation using Google GenAI (Gemini)."""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        max_tokens: int = 10000,
        temperature: float = 0.1,
        max_pages_per_request: int | None = 5,
        max_retries: int = 8,
        cost_tracker: "UsageTracker | None" = None,
        thinking_level: str | None = None,
    ) -> None:
        self._model_name = _model_name_for_genai(model_name)
        self._full_model_name = (
            model_name if model_name.startswith("gemini/") else f"gemini/{model_name}"
        )
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_pages = max_pages_per_request
        self._max_retries = max_retries
        self._cost_tracker = cost_tracker
        self._thinking_level = (thinking_level or "").strip().lower() or None
        self._sends_sampling_params = not is_gemini_3_or_newer(self._model_name)

    def _generation_config(self, **kwargs) -> types.GenerateContentConfig:
        """Build the per-request config.

        ``temperature`` is only sent to pre-Gemini-3 models; ``thinking_level``
        (minimal/low/medium/high) is sent when configured, otherwise the model
        default applies. ``thinking_budget`` is never sent.
        """
        if self._sends_sampling_params:
            kwargs["temperature"] = self._temperature
        if self._thinking_level:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=self._thinking_level
            )
        return types.GenerateContentConfig(max_output_tokens=self._max_tokens, **kwargs)

    def set_cost_tracker(self, cost_tracker: "UsageTracker | None") -> None:
        """Set cost tracker for recording usage (per-parse)."""
        self._cost_tracker = cost_tracker

    def _get_client(self) -> genai.Client:
        return genai.Client(api_key=self._api_key)

    def _record_usage(self, response, *, phase: str) -> None:
        """Record usage to cost_tracker if configured (*phase*: ``ocr`` or ``extraction``)."""
        if not self._cost_tracker:
            return
        um = getattr(response, "usage_metadata", None)
        if not um:
            return
        pt = (
            getattr(um, "prompt_token_count", None)
            or getattr(um, "input_tokens", 0)
            or 0
        )
        ct = (
            getattr(um, "candidates_token_count", None)
            or getattr(um, "output_tokens", 0)
            or 0
        )
        tt = getattr(um, "total_token_count", 0) - pt - ct if hasattr(um, "total_token_count") else 0
        add_fn = (
            self._cost_tracker.add_extraction_usage
            if phase == "extraction"
            else self._cost_tracker.add_ocr_usage
        )
        add_fn(self._full_model_name, pt, ct, max(0, tt))

    def _generate_with_retry(self, contents: list) -> str:
        """Call generate_content with retries on 503 and connection errors."""
        client = self._get_client()
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                response = client.models.generate_content(
                    model=self._model_name,
                    contents=contents,
                    config=self._generation_config(),
                )
                self._record_usage(response, phase="ocr")
                return (response.text or "").strip()
            except (genai_errors.ServerError, httpx.ConnectError) as e:
                last_exc = e
                if attempt == self._max_retries - 1:
                    logger.warning(
                        "Gemini request failed after %d attempts: %s",
                        self._max_retries,
                        last_exc,
                    )
                    raise
                delay = min(2**attempt, 60)
                logger.warning(
                    "Gemini request failed (attempt %d/%d): %s; retrying in %.0fs",
                    attempt + 1,
                    self._max_retries,
                    e,
                    delay,
                )
                time.sleep(delay)
        if last_exc is not None:
            raise last_exc
        return ""

    def ocr(self, content: bytes, mime: str, prompt: str) -> str:
        """OCR a single document (PDF or image). Returns markdown."""
        part = types.Part.from_bytes(data=content, mime_type=mime)
        return self._generate_with_retry([prompt, part])

    def ocr_pages(
        self, content: bytes, page_numbers: list[int], prompt: str
    ) -> dict[int, str]:
        """OCR specific pages of a PDF. Returns {page_no: markdown}."""
        extracted = _extract_pdf_page_bytes(content, page_numbers)
        if not extracted:
            return {}

        result: dict[int, str] = {}

        for page_no, pdf_bytes in extracted:
            part = types.Part.from_bytes(
                data=pdf_bytes, mime_type="application/pdf"
            )
            try:
                md = self._generate_with_retry([prompt, part])
                if md:
                    result[page_no] = md
            except Exception:
                pass

        return result

    def ocr_images(
        self, prompt: str, images: list[bytes], mime: str = "image/png"
    ) -> str:
        """OCR multiple images in one request. Returns combined markdown."""
        if not images:
            return ""
        parts: list = [prompt]
        for img_bytes in images:
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
        return self._generate_with_retry(parts)

    def ocr_batch(
        self, items: list[tuple[bytes, str]], prompt: str
    ) -> list[str]:
        """OCR a batch of (content_bytes, mime) items. Returns list of markdown strings."""
        results: list[str] = []

        for content_bytes, mime in items:
            part = types.Part.from_bytes(data=content_bytes, mime_type=mime)
            try:
                md = self._generate_with_retry([prompt, part])
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
        contents = [f"{instr}\n\nDocument:\n{text}"]
        return self._extract_impl(
            contents, schema, raise_on_validation_error=raise_on_validation_error
        )

    def extract_json(self, text: str, prompt: str) -> dict:
        """Schema-less JSON extraction: prompt + document text -> plain dict."""
        client = self._get_client()
        contents = [f"{prompt}\n\nDocument:\n{text}"]
        response = client.models.generate_content(
            model=self._model_name,
            contents=contents,
            config=self._generation_config(response_mime_type="application/json"),
        )
        self._record_usage(response, phase="extraction")
        raw_text = response.text or ""
        finish_reason = None
        try:
            finish_reason = str(response.candidates[0].finish_reason) if response.candidates else "no_candidates"
        except Exception:
            finish_reason = "unknown"
        logger.info(
            "extract_json raw response: model=%s finish_reason=%s raw_text_len=%d preview=%s",
            self._model_name,
            finish_reason,
            len(raw_text),
            raw_text[:500] if raw_text else "(empty)",
        )
        return safe_json_loads(
            raw_text,
            error_prefix="Gemini returned invalid JSON for freeform extraction",
        )

    def supports_bytes(self, mime: str) -> bool:
        """Gemini accepts PDFs and the common image MIMEs as raw bytes."""
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

        Gemini supports PDF and common image MIMEs directly. For other
        MIME types (EML, DOCX, XLSX, ODT, …), pre-flight with
        :meth:`supports_bytes` and parse to markdown first; calling this
        method with an unsupported *mime* raises
        :class:`UnsupportedBytesMimeError`.
        """
        if not self.supports_bytes(mime):
            raise UnsupportedBytesMimeError(mime, "Gemini")
        instr = prompt or DEFAULT_EXTRACTION_PROMPT
        part = types.Part.from_bytes(data=content, mime_type=mime)
        contents = [instr, part]
        return self._extract_impl(
            contents, schema, raise_on_validation_error=raise_on_validation_error
        )

    def _extract_impl(
        self,
        contents: list,
        schema: type[T],
        *,
        raise_on_validation_error: bool = True,
    ) -> T:
        """Call Gemini with structured output and return validated Pydantic instance.

        Falls back to prompt-only JSON mode when the schema contains types
        Gemini does not support (e.g. Dict[str, Any] which produces
        ``additionalProperties`` in JSON Schema).
        """
        client = self._get_client()
        try:
            response = client.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=self._generation_config(
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
        except Exception as e:
            if "additionalProperties" not in str(e):
                raise
            logger.warning(
                "Gemini rejected response_schema (%s); retrying without schema constraint",
                e,
            )
            response = client.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=self._generation_config(response_mime_type="application/json"),
            )
        self._record_usage(response, phase="extraction")
        raw_text = response.text or ""
        data = safe_json_loads(
            raw_text,
            error_prefix="Gemini returned invalid JSON for schema extraction",
        )

        return validate_or_raise_raw(
            schema, data, raise_on_validation_error=raise_on_validation_error
        )
