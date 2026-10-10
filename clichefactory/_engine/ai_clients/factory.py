"""Factory for creating AIClient instances from config."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from clichefactory._engine.config.base_config import AioConfig
    from clichefactory._engine.ai_clients.protocol import AIClient


def create_ai_client(
    config: "AioConfig",
    purpose: Literal["ocr", "extraction"] = "ocr",
) -> "AIClient":
    """
    Create an AIClient from config.

    - purpose="ocr": uses ocr_llm_model_name, ocr_llm_api_key
    - purpose="extraction": uses extraction_llm_model_name, extraction_llm_api_key
      (falls back to ocr_llm_* if extraction config is empty)
    """
    if purpose == "ocr":
        model = config.ocr_llm_model_name.strip()
        api_key = config.ocr_llm_api_key
        api_base = (getattr(config, "ocr_llm_api_base", "") or "").strip()
        max_tokens = int(getattr(config, "ocr_llm_max_tokens", 10000))
        temperature = float(getattr(config, "ocr_llm_temperature", 0.1))
        num_retries = int(getattr(config, "ocr_llm_num_retries", 8))
        thinking_level = getattr(config, "ocr_llm_thinking_level", None)
    else:
        model = (config.extraction_llm_model_name or "").strip()
        api_key = config.extraction_llm_api_key or config.ocr_llm_api_key
        api_base = (getattr(config, "extraction_llm_api_base", "") or "").strip()
        max_tokens = int(getattr(config, "extraction_llm_max_tokens", 10000))
        temperature = float(getattr(config, "extraction_llm_temperature", 0.1))
        num_retries = int(getattr(config, "extraction_llm_num_retries", 8))
        thinking_level = getattr(config, "extraction_llm_thinking_level", None)
        if not model:
            model = config.ocr_llm_model_name.strip()
            api_key = config.ocr_llm_api_key
            api_base = (getattr(config, "ocr_llm_api_base", "") or "").strip()
            max_tokens = int(getattr(config, "ocr_llm_max_tokens", 10000))
            temperature = float(getattr(config, "ocr_llm_temperature", 0.1))
            num_retries = int(getattr(config, "ocr_llm_num_retries", 8))
            thinking_level = getattr(config, "ocr_llm_thinking_level", None)

    if not (model or "").strip():
        raise ValueError(
            "LLM model name is empty. Configure extraction via LLM_MODEL_NAME + LLM_API_KEY "
            "or factory(model=Endpoint(provider_model=..., api_key=...))."
        )

    # Unprefixed names default to Gemini for backward compatibility (requires non-empty model above).
    if not model.startswith(("gemini/", "openai/", "anthropic/", "ollama/")):
        model = f"gemini/{model}"

    if model.startswith("gemini/"):
        from clichefactory._engine.ai_clients.gemini_client import GeminiAIClient

        return GeminiAIClient(
            model_name=model,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=num_retries,
            thinking_level=thinking_level,
        )
    if model.startswith("openai/"):
        from clichefactory._engine.ai_clients.openai_client import OpenAIAIClient

        return OpenAIAIClient(
            model_name=model,
            api_key=api_key,
            api_base=api_base or None,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=num_retries,
        )
    if model.startswith("anthropic/"):
        from clichefactory._engine.ai_clients.anthropic_client import AnthropicAIClient

        return AnthropicAIClient(
            model_name=model,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=num_retries,
        )
    if model.startswith("ollama/"):
        if purpose == "ocr":
            raise ValueError(
                "Ollama is extraction-only in MVP and is not supported for OCR purpose. "
                "Use gemini/openai/anthropic for OCR, or select a non-LLM OCR parser."
            )
        from clichefactory._engine.ai_clients.ollama_client import OllamaAIClient

        if not api_base:
            api_base = "http://localhost:11434"
        return OllamaAIClient(
            model_name=model,
            api_base=api_base,
            api_key=api_key or "",
        )

    raise ValueError(
        f"Unknown AI provider for model {model}. "
        "Use prefix: gemini/, openai/, anthropic/, or ollama/"
    )
