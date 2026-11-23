"""clichefactory._schema: same behaviour as clichefactory_internal.contracts.model_schema."""
import pydantic
import pytest
import jsonschema

from clichefactory._schema import (
    batch_config_to_canonical,
    canonical_schema_to_pydantic,
    validate_model_schema,
)


def test_validate_model_schema_valid():
    validate_model_schema({"type": "object", "properties": {"x": {"type": "string"}}})


def test_validate_model_schema_invalid():
    with pytest.raises((jsonschema.SchemaError, jsonschema.ValidationError, TypeError)):
        validate_model_schema("not a dict")


def test_batch_config_to_canonical():
    config = {
        "root_model": "Invoice",
        "definitions": [
            {
                "name": "Invoice",
                "fields": [
                    {"name": "number", "type": "text", "required": True},
                    {"name": "total", "type": "float"},
                ],
            },
        ],
    }
    out = batch_config_to_canonical(config)
    assert out["type"] == "object"
    assert "properties" in out
    assert "number" in out["properties"]
    assert out["properties"]["number"]["type"] == "string"
    assert out["properties"]["total"]["type"] == "number"
    assert "$defs" in out
    assert "Invoice" in out["$defs"]


def test_batch_config_to_canonical_list_of_models():
    config = {
        "root_model": "Doc",
        "definitions": [
            {"name": "Item", "fields": [{"name": "name", "type": "text"}]},
            {"name": "Doc", "fields": [{"name": "items", "type": "list_of_models", "target_model": "Item"}]},
        ],
    }
    out = batch_config_to_canonical(config)
    assert out["$defs"]["Doc"]["properties"]["items"]["type"] == "array"
    assert out["$defs"]["Doc"]["properties"]["items"]["items"]["$ref"] == "#/$defs/Item"


def test_canonical_schema_to_pydantic_simple():
    schema = {"type": "object", "properties": {"foo": {"type": "string"}, "bar": {"type": "integer"}}}
    model = canonical_schema_to_pydantic(schema, "Simple")
    inst = model(foo="x", bar=1)
    assert inst.foo == "x"
    assert inst.bar == 1


def test_canonical_schema_to_pydantic_simple_format():
    """Simple format: keys are field names, values are type strings."""
    schema = {"invoice_number": "string", "total": "number"}
    model = canonical_schema_to_pydantic(schema, "Invoice")
    inst = model(invoice_number="INV-1", total=10.5)
    assert inst.invoice_number == "INV-1"
    assert inst.total == 10.5


def test_canonical_schema_to_pydantic_roundtrip():
    config = {
        "root_model": "R",
        "definitions": [{"name": "R", "fields": [{"name": "a", "type": "text"}, {"name": "b", "type": "integer"}]}],
    }
    canonical = batch_config_to_canonical(config)
    model = canonical_schema_to_pydantic(canonical, "R")
    inst = model(a="hello", b=42)
    assert inst.a == "hello"
    assert inst.b == 42


def _described_config():
    return {
        "root_model": "Invoice",
        "definitions": [
            {
                "name": "Line",
                "fields": [
                    {"name": "amount", "type": "float", "description": "Net amount, without VAT"},
                ],
            },
            {
                "name": "Invoice",
                "fields": [
                    {"name": "number", "type": "text", "description": "  Invoice number from the header  "},
                    {"name": "issued", "type": "date", "required": False, "description": "Issue date"},
                    {"name": "note", "type": "text", "description": "   "},
                    {"name": "plain", "type": "integer"},
                    {
                        "name": "lines",
                        "type": "list_of_models",
                        "target_model": "Line",
                        "description": "One entry per invoice line",
                    },
                ],
            },
        ],
    }


def test_batch_config_to_canonical_carries_field_descriptions():
    out = batch_config_to_canonical(_described_config())
    props = out["properties"]
    assert props["number"] == {"type": "string", "description": "Invoice number from the header"}
    assert props["issued"] == {"type": "string", "format": "date", "description": "Issue date"}
    assert props["lines"]["description"] == "One entry per invoice line"
    assert props["lines"]["items"] == {"$ref": "#/$defs/Line"}
    assert out["$defs"]["Line"]["properties"]["amount"]["description"] == "Net amount, without VAT"
    # Blank and missing descriptions add nothing.
    assert props["note"] == {"type": "string"}
    assert props["plain"] == {"type": "integer"}


def test_batch_config_to_canonical_does_not_mutate_dict_types():
    raw_type = {"type": "string", "maxLength": 5}
    config = {
        "root_model": "R",
        "definitions": [{"name": "R", "fields": [{"name": "a", "type": raw_type, "description": "A"}]}],
    }
    out = batch_config_to_canonical(config)
    assert out["properties"]["a"] == {"type": "string", "maxLength": 5, "description": "A"}
    assert raw_type == {"type": "string", "maxLength": 5}


def test_canonical_schema_to_pydantic_carries_field_descriptions():
    model = canonical_schema_to_pydantic(batch_config_to_canonical(_described_config()), "Invoice")
    fields = model.model_fields
    assert fields["number"].description == "Invoice number from the header"
    assert fields["number"].is_required()
    assert fields["issued"].description == "Issue date"
    assert not fields["issued"].is_required()
    assert fields["lines"].description == "One entry per invoice line"
    assert fields["note"].description is None
    assert fields["plain"].description is None
    line_model = fields["lines"].annotation.__args__[0]
    assert line_model.model_fields["amount"].description == "Net amount, without VAT"

    json_schema = model.model_json_schema()
    assert json_schema["properties"]["number"]["description"] == "Invoice number from the header"
    inst = model(number="INV-1", plain=1, note="x", lines=[{"amount": 2.5}])
    assert inst.issued is None
    assert inst.lines[0].amount == 2.5


def test_canonical_schema_to_pydantic_without_descriptions_is_unchanged():
    """No descriptions in, no descriptions out: existing prompts stay byte-identical."""
    config = {
        "root_model": "R",
        "definitions": [{"name": "R", "fields": [{"name": "a", "type": "text"}, {"name": "b", "type": "integer", "required": False}]}],
    }
    json_schema = canonical_schema_to_pydantic(batch_config_to_canonical(config), "R").model_json_schema()
    assert json_schema["properties"]["a"] == {"title": "A", "type": "string"}
    assert "description" not in json_schema["properties"]["b"]


# --- boolean field type ------------------------------------------------------


@pytest.mark.parametrize("ftype", ["boolean", "bool", "Boolean"])
def test_batch_config_boolean_field(ftype):
    config = {
        "root_model": "R",
        "definitions": [{"name": "R", "fields": [{"name": "is_registration", "type": ftype}]}],
    }
    out = batch_config_to_canonical(config)
    assert out["properties"]["is_registration"] == {"type": "boolean"}
    model = canonical_schema_to_pydantic(out, "R")
    assert model.model_fields["is_registration"].annotation is bool
    assert model(is_registration=True).is_registration is True
    assert model.model_json_schema()["properties"]["is_registration"]["type"] == "boolean"


@pytest.mark.parametrize("ftype", ["yes_no", "datetime", 5])
def test_batch_config_unknown_type_raises(ftype):
    """An unknown type is an error, never a silent string."""
    config = {
        "root_model": "R",
        "definitions": [{"name": "R", "fields": [{"name": "flag", "type": ftype}]}],
    }
    with pytest.raises(ValueError, match="flag"):
        batch_config_to_canonical(config)


def test_batch_config_missing_type_is_text():
    config = {"root_model": "R", "definitions": [{"name": "R", "fields": [{"name": "a"}]}]}
    assert batch_config_to_canonical(config)["properties"]["a"] == {"type": "string"}


# --- enum and value constraints ------------------------------------------------


def _vehicle_schema():
    return {
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
            "mass_kg": {"type": "number", "minimum": 0, "exclusiveMinimum": 0},
            "photo_ok": {"type": "boolean"},
            "fuel": {"type": "string", "enum": ["petrol", "diesel", "electric"]},
            "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
        },
        "required": ["document_type", "vin", "plate", "seats", "mass_kg", "photo_ok", "tags"],
    }


def _valid_vehicle():
    return {
        "document_type": "registration_certificate",
        "vin": "WBA12345678901234",
        "plate": "LJ AB-123",
        "seats": 5,
        "mass_kg": 1500.5,
        "photo_ok": True,
        "tags": ["a"],
    }


def test_enum_and_constraints_round_trip():
    model = canonical_schema_to_pydantic(_vehicle_schema(), "Vehicle")
    inst = model(**_valid_vehicle())
    assert inst.document_type == "registration_certificate"
    assert inst.fuel is None

    props = model.model_json_schema()["properties"]
    assert props["document_type"]["enum"] == ["registration_certificate", "insurance_policy", "other"]
    assert props["document_type"]["description"] == "Kind of document"
    assert props["vin"]["pattern"] == "^[A-HJ-NPR-Z0-9]{17}$"
    assert (props["plate"]["minLength"], props["plate"]["maxLength"]) == (4, 10)
    assert (props["seats"]["minimum"], props["seats"]["maximum"]) == (1, 9)
    # exclusive bounds are ignored (Gemini's response schema rejects them).
    assert "exclusiveMinimum" not in props["mass_kg"]
    assert model(**{**_valid_vehicle(), "mass_kg": 0}).mass_kg == 0
    assert props["photo_ok"]["type"] == "boolean"
    # Optional enum: offered to the model, null still allowed.
    assert {"enum": ["petrol", "diesel", "electric"], "type": "string"} in props["fuel"]["anyOf"]
    assert props["tags"]["items"]["enum"] == ["a", "b"]


@pytest.mark.parametrize(
    "field, value, error_type",
    [
        ("document_type", "passport", "literal_error"),
        ("vin", "WBA1234567890123I", "string_pattern_mismatch"),  # I is not allowed in a VIN
        ("vin", "WBA123", "string_pattern_mismatch"),
        ("plate", "LJ", "string_too_short"),
        ("plate", "LJ AB-123-456", "string_too_long"),
        ("seats", 0, "greater_than_equal"),
        ("seats", 12, "less_than_equal"),
        ("mass_kg", -1, "greater_than_equal"),
        ("fuel", "steam", "literal_error"),
        ("tags", ["c"], "literal_error"),
    ],
)
def test_constraint_violation_is_a_validation_error(field, value, error_type):
    """These are the errors that become ``validation_errors`` / ``status: partial``."""
    model = canonical_schema_to_pydantic(_vehicle_schema(), "Vehicle")
    data = {**_valid_vehicle(), field: value}
    with pytest.raises(pydantic.ValidationError) as excinfo:
        model.model_validate(data)
    errors = excinfo.value.errors()
    assert [e["type"] for e in errors] == [error_type]
    assert errors[0]["loc"][0] == field


def test_pattern_is_unanchored_like_json_schema():
    """JSON Schema patterns match anywhere unless anchored; look-arounds are allowed."""
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "pattern": "[0-9]{3}"},
            "ref": {"type": "string", "pattern": "^(?!X)[A-Z]+$"},
        },
        "required": ["code", "ref"],
    }
    model = canonical_schema_to_pydantic(schema, "P")
    assert model(code="ab123cd", ref="ABC").code == "ab123cd"
    with pytest.raises(pydantic.ValidationError):
        model(code="ab123cd", ref="XYZ")


def test_enum_constraint_on_nested_ref_model():
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}}},
        "required": ["items"],
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"unit": {"type": "string", "enum": ["kg", "pcs"]}},
                "required": ["unit"],
            }
        },
    }
    model = canonical_schema_to_pydantic(schema, "Doc")
    assert model(items=[{"unit": "kg"}]).items[0].unit == "kg"
    with pytest.raises(pydantic.ValidationError):
        model(items=[{"unit": "litre"}])


def test_non_string_enum_and_constraints_ignored_for_other_types():
    schema = {
        "type": "object",
        "properties": {
            "level": {"type": "integer", "enum": [1, 2, 3]},
            "count": {"type": "integer", "pattern": "^[0-9]+$"},  # pattern applies to strings only
        },
        "required": ["level", "count"],
    }
    model = canonical_schema_to_pydantic(schema, "N")
    assert model(level=2, count=10).level == 2
    with pytest.raises(pydantic.ValidationError):
        model(level=4, count=10)


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "string", "pattern": "("},
        {"type": "string", "minLength": -1},
        {"type": "string", "maxLength": "5"},
        {"type": "integer", "minimum": "1"},
    ],
)
def test_malformed_constraint_raises_value_error(prop):
    schema = {"type": "object", "properties": {"x": prop}, "required": ["x"]}
    with pytest.raises(ValueError, match=r"\.x"):
        canonical_schema_to_pydantic(schema, "Bad")


def test_dict_typed_batch_field_keeps_constraints():
    """A BatchConfig field may carry a raw JSON Schema type; its keywords now reach the model."""
    config = {
        "root_model": "R",
        "definitions": [
            {"name": "R", "fields": [{"name": "kind", "type": {"type": "string", "enum": ["a", "b"]}}]}
        ],
    }
    model = canonical_schema_to_pydantic(batch_config_to_canonical(config), "R")
    with pytest.raises(pydantic.ValidationError):
        model(kind="c")


def test_schema_without_constraints_is_unchanged():
    """No enum/constraint keywords in, the same JSON schema (and prompt) out as before."""
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
            "c": {"type": "boolean"},
            "d": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["a", "c", "d"],
    }
    json_schema = canonical_schema_to_pydantic(schema, "R").model_json_schema()
    assert json_schema == {
        "properties": {
            "a": {"title": "A", "type": "string"},
            "b": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "B"},
            "c": {"title": "C", "type": "boolean"},
            "d": {"items": {"type": "string"}, "title": "D", "type": "array"},
        },
        "required": ["a", "c", "d"],
        "title": "R",
        "type": "object",
    }
