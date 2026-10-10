"""
Base configuration for AIO pipeline.
TrainingConfig inherits from this and adds training-specific fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from clichefactory._engine.cache.base_cacher import Cacher


@dataclass
class AioConfig:
    """Shared configuration for OCR, extraction, PDF/image parsing, and costs."""

    # --- OCR LLM (for VLM parsers and per-page fallbacks) ---
    ocr_llm_model_name: str
    ocr_llm_api_key: str
    ocr_llm_api_base: str = ""
    ocr_llm_max_tokens: int = 10000
    ocr_llm_temperature: float = 1.0
    ocr_llm_num_retries: int = 8
    # Gemini 3+ thinking level ("minimal" | "low" | "medium" | "high"); None = model default.
    ocr_llm_thinking_level: Optional[str] = None

    # --- Extraction LLM (for DSPy / data extraction) ---
    extraction_llm_model_name: str = ""
    extraction_llm_api_key: str = ""
    extraction_llm_api_base: str = ""
    extraction_llm_max_tokens: int = 10000
    extraction_llm_temperature: float = 1.0
    extraction_llm_num_retries: int = 8
    extraction_llm_thinking_level: Optional[str] = None

    # --- PDF: image path strategy when PDF is classified as image ---
    pdf_image_parser: Literal[
        "docling", "docling_vlm", "ocr_llm", "yolo_per_partes"
    ] = "docling"
    pdf_fallback_to_ocr_llm: bool = True
    # If True, when PyMuPDF structured strategy fails, retry with image strategy
    pdf_structured_fallback_to_image: bool = False
    # OCR engine for Docling PDF pipelines (tesseract, rapidocr, easyocr)
    pdf_ocr_engine: Literal["tesseract", "rapidocr", "easyocr"] = "rapidocr"
    pdf_ocr_lang: str = "eng"
    # Use AIClient for body extraction in parser strategies that support it
    use_ocr_llm_body: bool = True

    # --- Image parsers ---
    image_parser: Literal["pytesseract", "rapidocr", "docling", "ocr_llm"] = "rapidocr"
    image_parser_fallback: bool = True
    image_parser_lang: str = "eng"

    # --- Costs ---
    cost_tracking_enabled: bool = False
    model_pricing_path: Optional[str] = None
    # Optional external usage tracker (typically provided by server control-plane)
    usage_tracker: Optional[Any] = None

    # --- Caching ---
    cacher: Optional[Cacher] = None
