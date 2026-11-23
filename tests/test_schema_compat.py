"""Extraction schemas with enum / value constraints reach each vendor in a form it accepts."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from clichefactory._engine.ai_clients.schema_compat import (
    ANTHROPIC_UNSUPPORTED,
    OPENAI_UNSUPPORTED,
    has_keywords,
    move_to_description,
)
from clichefactory._schema import canonical_schema_to_pydantic

VEHICLE = {
    "type": "object",
    "properties": {
        "document_type": {
            "type": "string",
            "enum": ["registration_certificate", "insurance_policy", "other"],
            "description": "Kind of document",
        },
        "vin": {"type": "string", "pattern": "^[A-HJ-NPR-Z0-9]{17}$"},
        "plate": {"type": "string", "minLength": 4, "maxLength": 10},
        "seats": {"type": "integer", "minimum": 1, "maximum": 9},
        "photo_ok": {"type": "boolean"},
        "fuel": {"type": "string", "enum": ["petrol", "diesel"]},
        "owners": {"type": "array", "items": {"$ref": "#/$defs/Owner"}},
    },
    "required": ["document_type", "vin", "plate", "seats", "photo_ok", "owners"],
    "$defs": {
        "Owner": {
            "type": "object",
            "properties": {"tax_number": {"type": "string", "pattern": "^[0-9]{8}$"}},
            "required": ["tax_number"],
        }
    },
}

PLAIN = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "b": {"type": "integer"}, "c": {"type": "boolean"}},
    "required": ["a"],
}


def _model(schema=VEHICLE):
    return canonical_schema_to_pydantic(schema, "Vehicle")


def _nodes(schema):
    """Every property schema in a model_json_schema(), flattened by name."""
    out = {}
    for name, prop in schema.get("properties", {}).items():
        out[name] = prop
    for d in schema.get("$defs", {}).values():
        out.update(_nodes(d))
    return out


# --- move_to_description -----------------------------------------------------


def test_move_to_description_moves_only_the_given_keywords():
    js = _model().model_json_schema()
    out = move_to_description(js, ANTHROPIC_UNSUPPORTED)
    props = _nodes(out)
    assert "pattern" not in props["vin"]
    assert props["vin"]["description"] == "Must match the regular expression ^[A-HJ-NPR-Z0-9]{17}$"
    assert props["plate"]["description"] == "At least 4 characters; at most 10 characters"
    assert props["seats"]["description"] == "At least 1; at most 9"
    assert props["tax_number"]["description"] == "Must match the regular expression ^[0-9]{8}$"
    # enum stays a real constraint, existing descriptions are kept.
    assert props["document_type"]["enum"] == ["registration_certificate", "insurance_policy", "other"]
    assert props["document_type"]["description"] == "Kind of document"
    assert not has_keywords(out, ANTHROPIC_UNSUPPORTED)
    # The input is not modified.
    assert _nodes(js)["vin"]["pattern"] == "^[A-HJ-NPR-Z0-9]{17}$"


def test_move_to_description_appends_to_an_existing_description():
    out = move_to_description(
        {"type": "string", "description": "Plate", "maxLength": 10}, OPENAI_UNSUPPORTED
    )
    assert out == {"type": "string", "description": "Plate (at most 10 characters)"}


def test_property_named_like_a_keyword_is_left_alone():
    schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "minimum": {"type": "integer"}},
        "required": ["pattern", "minimum"],
    }
    assert not has_keywords(schema, ANTHROPIC_UNSUPPORTED)
    assert move_to_description(schema, ANTHROPIC_UNSUPPORTED) == schema


def test_schema_without_constraints_is_unchanged():
    js = _model(PLAIN).model_json_schema()
    assert move_to_description(js, ANTHROPIC_UNSUPPORTED) == js


# --- Anthropic ---------------------------------------------------------------


def test_anthropic_output_schema_has_no_unsupported_keywords():
    from clichefactory._engine.ai_clients.anthropic_client import _output_schema

    out = _output_schema(_model())
    assert not has_keywords(out, ANTHROPIC_UNSUPPORTED)
    assert out["additionalProperties"] is False
    assert out["$defs"]["Vehicle_ownersItem"]["additionalProperties"] is False
    assert _nodes(out)["document_type"]["enum"][0] == "registration_certificate"


def test_anthropic_output_schema_unchanged_without_constraints():
    from clichefactory._engine.ai_clients.anthropic_client import (
        _add_additional_properties_false,
        _output_schema,
    )

    model = _model(PLAIN)
    assert _output_schema(model) == _add_additional_properties_false(model.model_json_schema())


def test_anthropic_reply_still_validated_against_constraints():
    """A reply that breaks a moved constraint is a validation error, i.e. a partial result."""
    from unittest.mock import MagicMock

    from clichefactory._engine.ai_clients.anthropic_client import AnthropicAIClient
    from clichefactory._extract_validation import RawExtractionValidationError

    client = AnthropicAIClient.__new__(AnthropicAIClient)
    client._model_name = "claude-test"
    client._max_tokens = 100
    client._temperature = 0
    client._cost_tracker = None
    block = MagicMock()
    block.text = (
        '{"document_type": "other", "vin": "SHORT", "plate": "LJ AB-1", "seats": 5,'
        ' "photo_ok": true, "fuel": null, "owners": []}'
    )
    response = MagicMock(content=[block])
    client._messages_create = MagicMock(return_value=response)

    with pytest.raises(RawExtractionValidationError) as excinfo:
        client._extract_impl("prompt", _model(), raise_on_validation_error=False)
    assert [e["loc"] for e in excinfo.value.validation_errors] == [("vin",)]
    sent = client._messages_create.call_args.kwargs["output_config"]["format"]["schema"]
    assert not has_keywords(sent, ANTHROPIC_UNSUPPORTED)


# --- OpenAI ------------------------------------------------------------------


def test_openai_response_format_is_the_class_without_length_bounds():
    from clichefactory._engine.ai_clients.openai_client import _response_format

    model = _model(PLAIN)
    assert _response_format(model) is model
    # pattern and numeric bounds are accepted by OpenAI, so they don't force the dict form.
    no_lengths = {**VEHICLE, "properties": {k: v for k, v in VEHICLE["properties"].items() if k != "plate"}}
    no_lengths["required"] = [r for r in VEHICLE["required"] if r != "plate"]
    model = _model(no_lengths)
    assert _response_format(model) is model


def test_openai_response_format_moves_length_bounds():
    from clichefactory._engine.ai_clients.openai_client import _response_format

    fmt = _response_format(_model())
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    sent = fmt["json_schema"]["schema"]
    assert not has_keywords(sent, OPENAI_UNSUPPORTED)
    props = _nodes(sent)
    assert props["plate"]["description"] == "At least 4 characters; at most 10 characters"
    assert props["vin"]["pattern"] == "^[A-HJ-NPR-Z0-9]{17}$"
    assert (props["seats"]["minimum"], props["seats"]["maximum"]) == (1, 9)


# --- Gemini ------------------------------------------------------------------


def test_gemini_accepts_the_constrained_model():
    """google-genai validates the response schema before sending; it must not reject ours."""
    from google.genai import _transformers

    gemini_schema = _transformers.t_schema(None, _model()).model_dump(exclude_none=True)
    props = gemini_schema["properties"]
    assert props["document_type"]["enum"] == ["registration_certificate", "insurance_policy", "other"]
    assert props["vin"]["pattern"] == "^[A-HJ-NPR-Z0-9]{17}$"
    assert (props["plate"]["min_length"], props["plate"]["max_length"]) == (4, 10)
    assert (props["seats"]["minimum"], props["seats"]["maximum"]) == (1, 9)


def test_model_enforces_what_vendors_only_see_as_hints():
    model = _model()
    with pytest.raises(ValidationError):
        model.model_validate(
            {"document_type": "other", "vin": "WBA12345678901234", "plate": "LJ", "seats": 5,
             "photo_ok": True, "owners": []}
        )
