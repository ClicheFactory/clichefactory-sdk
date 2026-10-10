from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

PostprocessFn = Callable[[dict[str, Any]], dict[str, Any]]
"""Type alias for a user-supplied postprocessing hook.

A ``PostprocessFn`` is called on the raw extraction result dict *after* the
built-in numeric coercion and *before* Pydantic validation::

    raw LLM dict → [system coerce] → [PostprocessFn] → Pydantic validate

The function receives and must return a ``dict[str, Any]``.  It can modify
values in-place or return a new dict.

Example::

    def normalise_dates(result: dict) -> dict:
        if "invoice_date" in result:
            result["invoice_date"] = parse_date(result["invoice_date"])
        return result

    cliche = client.cliche(Invoice, postprocess=normalise_dates)
"""


@dataclass(frozen=True, slots=True)
class PartialExtraction:
    """Extraction payload when the model output did not fully match the schema.

    Returned when ``allow_partial=True`` and validation failed server-side or
    locally; ``raw`` holds the coerced/postprocessed dict, and
    ``validation_errors`` is the Pydantic error list.
    """

    raw: dict[str, Any]
    validation_errors: list[dict[str, Any]]


ThinkingLevel = Literal["minimal", "low", "medium", "high"]


class Endpoint(BaseModel):
    """BYOK endpoint configuration (maps to server ModelEndpointPayload).

    ``thinking_level`` applies to Gemini 3+ models (supported levels vary by
    model; unset = model default). ``temperature`` is not sent to Gemini 3+
    models, which ignore it and will soon reject it.
    """

    model_config = ConfigDict(extra="forbid")

    provider_model: str | None = None
    api_key: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    num_retries: int | None = Field(default=None, ge=0)
    api_base: str | None = None
    thinking_level: ThinkingLevel | None = None


PdfImageParser = Literal["docling", "docling_vlm", "ocr_llm", "vision_layout"]
PdfOcrEngine = Literal["tesseract", "rapidocr", "easyocr"]
ImageParser = Literal["pytesseract", "rapidocr", "docling", "ocr_llm"]


class ParsingOptions(BaseModel):
    """Public parsing options. Mirrors `aio.config.AioConfig` / server ParsingPayload."""

    model_config = ConfigDict(extra="forbid")

    # PDF
    pdf_image_parser: PdfImageParser | None = None
    pdf_fallback_to_ocr_llm: bool | None = None
    pdf_structured_fallback_to_image: bool | None = None
    pdf_ocr_engine: PdfOcrEngine | None = None
    pdf_ocr_lang: str | None = None
    use_ocr_llm_body: bool | None = None

    # Images
    image_parser: ImageParser | None = None
    image_parser_fallback: bool | None = None
    image_parser_lang: str | None = None


class CostInfo(BaseModel):
    """SDK-facing cost payload (flexible, supports service + BYOK)."""

    model_config = ConfigDict(extra="allow")

    total_usd: float | None = None
    breakdown: dict[str, Any] | None = None


class ExtractMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str | None = None
    mode: str | None = None
    parser: str | None = None
    deployment: dict[str, Any] | None = None


class ExtractEnvelope(BaseModel):
    """Optional rich extract return envelope."""

    model_config = ConfigDict(extra="allow")

    data: dict[str, Any]
    costs: CostInfo | None = None
    document: dict[str, Any] | None = None
    meta: ExtractMeta | None = None


class TrainingMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str | None = None


class TrainingMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")

    baseline_score_on_dev: float | None = None
    final_score_on_dev: float | None = None
    final_score_full_dataset: float | None = None


class TrainingResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    metrics: TrainingMetrics | None = None
    costs: CostInfo | None = None
    meta: TrainingMeta | None = None


# ── Long-document extraction ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Chunk:
    """A slice of a long document's markdown.

    ``page_start`` / ``page_end`` are 1-indexed and inclusive.  They are
    ``None`` when the chunker cannot determine page boundaries (e.g. a
    plain text input, or markdown without page markers).
    """

    index: int
    text: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: tuple[str, ...] = ()
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One field's value observed in one chunk's extraction result.

    ``value`` is ``None`` when the chunk produced no value for this field
    (missing, null, or an empty collection).  ``confidence`` is ``None``
    unless the underlying LLM provided logprobs/confidence data.
    """

    value: Any
    chunk: "Chunk"
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ResolverContext:
    """Context passed to a resolver alongside the per-chunk values."""

    field_name: str
    field_schema: dict[str, Any]
    all_chunks: tuple["Chunk", ...]


# ``ResolverFn`` is the low-level callable contract.  A resolver can also
# be a string alias (e.g. ``"first_non_null"``) or a pre-built factory
# result from ``clichefactory.resolvers`` — see ``Resolver`` below.
ResolverFn = Callable[[list["FieldValue"], "ResolverContext"], Any]
Resolver = ResolverFn | str
ResolverSpec = dict[str, Resolver]


@dataclass(frozen=True, slots=True)
class ResolutionTrace:
    """Per-field record of which resolver ran and what it saw.

    Useful for debugging long-document merges and for review UIs.
    """

    field_name: str
    resolver_name: str
    per_chunk_values: tuple["FieldValue", ...]
    winning_chunk_indices: tuple[int, ...]
    final_value: Any
    warnings: tuple[str, ...] = ()


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LongExtractionResult(Generic[T]):
    """Rich return type for ``Cliche.extract_long(include_chunk_results=True)``.

    When ``include_chunk_results=False`` (the default), ``extract_long`` returns
    just ``value`` directly so the signature matches ``extract``.
    """

    value: T
    chunks: tuple["Chunk", ...]
    per_chunk: tuple[Any, ...]
    per_field: dict[str, tuple["FieldValue", ...]]
    resolutions: dict[str, "ResolutionTrace"]
    warnings: tuple[str, ...] = ()
    cost: dict[str, Any] = field(default_factory=dict)

