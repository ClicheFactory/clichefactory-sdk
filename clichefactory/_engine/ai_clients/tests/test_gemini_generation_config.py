"""Gemini request config: no sampling params on Gemini 3+, thinking_level instead of thinking_budget."""
import pytest

from clichefactory._engine.ai_clients.factory import create_ai_client
from clichefactory._engine.ai_clients.gemini_client import (
    GeminiAIClient,
    is_gemini_3_or_newer,
)
from clichefactory._engine.config import AioConfig


@pytest.mark.parametrize(
    "model",
    [
        "gemini/gemini-3-flash-preview",
        "gemini-3-pro-preview",
        "gemini/gemini-3.6-flash",
        "gemini/gemini-3.8-flash",
        "gemini/gemini-4-pro",
        "gemini/gemini-flash-latest",
        "gemini/gemini-flash-lite-latest",
        "gemini/gemini-pro-latest",
    ],
)
def test_gemini_3_or_newer(model):
    assert is_gemini_3_or_newer(model)


@pytest.mark.parametrize(
    "model",
    [
        "gemini/gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini/gemini-2.5-pro-preview-06-05",
        "gemini/gemini-1.5-pro",
        "gemini/gemini-pro",
        "gemini/gemini-flash-2",
        "gemini/gemini-exp-1206",
        "gemini/gemma-3-27b-it",
    ],
)
def test_pre_gemini_3(model):
    assert not is_gemini_3_or_newer(model)


def _dump(client: GeminiAIClient, **kwargs) -> dict:
    return client._generation_config(**kwargs).model_dump(mode="json", exclude_none=True)


def test_gemini_3_omits_sampling_params():
    client = GeminiAIClient(
        model_name="gemini/gemini-3-flash-preview", api_key="k", temperature=0.2, max_tokens=123
    )
    cfg = _dump(client, response_mime_type="application/json")
    assert cfg == {"max_output_tokens": 123, "response_mime_type": "application/json"}


def test_gemini_2_keeps_temperature():
    client = GeminiAIClient(model_name="gemini/gemini-2.5-flash", api_key="k", temperature=0.2)
    assert _dump(client)["temperature"] == 0.2


def test_thinking_level_sent_as_thinking_level_not_budget():
    client = GeminiAIClient(
        model_name="gemini/gemini-3-flash-preview", api_key="k", thinking_level="Low"
    )
    cfg = _dump(client)
    assert cfg["thinking_config"] == {"thinking_level": "LOW"}
    assert "thinking_budget" not in cfg["thinking_config"]


def test_no_thinking_config_by_default():
    client = GeminiAIClient(model_name="gemini/gemini-3-flash-preview", api_key="k")
    assert "thinking_config" not in _dump(client)


def test_factory_passes_thinking_level_per_purpose():
    config = AioConfig(
        ocr_llm_model_name="gemini/gemini-3-flash-preview",
        ocr_llm_api_key="k",
        ocr_llm_thinking_level="minimal",
        extraction_llm_model_name="gemini/gemini-3-pro-preview",
        extraction_llm_api_key="k",
        extraction_llm_thinking_level="high",
    )
    assert create_ai_client(config, purpose="ocr")._thinking_level == "minimal"
    assert create_ai_client(config, purpose="extraction")._thinking_level == "high"


def test_factory_extraction_falls_back_to_ocr_thinking_level():
    config = AioConfig(
        ocr_llm_model_name="gemini/gemini-3-flash-preview",
        ocr_llm_api_key="k",
        ocr_llm_thinking_level="low",
    )
    assert create_ai_client(config, purpose="extraction")._thinking_level == "low"
