"""
Canonical inference payload: model_schema (JSON Schema), file identity, config, optional extractor.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InferenceConfigPayload(BaseModel):
    """LLM config for inference (extraction and optional OCR). Strict, no extra fields."""

    model_config = ConfigDict(extra="forbid")

    llm_model_name: str | None = None
    llm_max_tokens: int | None = Field(default=None, gt=0)
    llm_api_key: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_num_retries: int | None = Field(default=None, ge=0)
    llm_api_base: str | None = None
    extraction: "ModelEndpointPayload | None" = None
    ocr: "ModelEndpointPayload | None" = None
    extraction_mode: Literal["fast", "trained", "robust", "robust-trained"] | None = None
    parser: Literal["default", "docling", "docling-vlm", "ocr-llm"] | None = None


class ModelEndpointPayload(BaseModel):
    """Single endpoint config (provider_model, api_key, etc.)."""

    model_config = ConfigDict(extra="forbid")

    provider_model: str | None = None
    api_key: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    num_retries: int | None = Field(default=None, ge=0)
    api_base: str | None = None
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None


class InferencePayload(BaseModel):
    """Canonical inference (extraction) payload."""

    model_config = ConfigDict(extra="forbid")

    model_schema: dict[str, object] = Field(..., description="Canonical JSON Schema form")
    file_name: str
    file_type: str
    config: InferenceConfigPayload = Field(default_factory=InferenceConfigPayload)
    extractor_path: str | None = None


InferenceConfigPayload.model_rebuild()
