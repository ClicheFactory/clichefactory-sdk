"""Make an extraction JSON Schema acceptable to a vendor's structured-output API.

Extraction schemas may carry value constraints (``pattern``, ``minLength`` /
``maxLength``, ``minimum`` / ``maximum``). Some vendors reject some of these in a
response schema, which would fail the whole call. For those vendors the keywords
are moved out of the schema and into the field's ``description`` as a plain
hint, so the model is still told about them. They are still enforced: the
response is validated against the Pydantic model, which keeps every constraint,
so a value that breaks one shows up in ``validation_errors``.

A schema without any of the moved keywords comes back unchanged (as a copy).
"""
from __future__ import annotations

import copy
from typing import Any

# What Claude's structured outputs do not accept in a schema. Anthropic's own SDK
# also moves ``pattern`` into the description, so it is moved here too.
ANTHROPIC_UNSUPPORTED = frozenset(
    {
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }
)

# OpenAI strict structured outputs accept pattern and numeric bounds for strings
# and numbers, but not string length bounds.
OPENAI_UNSUPPORTED = frozenset({"minLength", "maxLength"})

_HINTS = {
    "pattern": "must match the regular expression {}",
    "minLength": "at least {} characters",
    "maxLength": "at most {} characters",
    "minimum": "at least {}",
    "maximum": "at most {}",
    "exclusiveMinimum": "greater than {}",
    "exclusiveMaximum": "less than {}",
    "multipleOf": "a multiple of {}",
}

# Keywords whose value is a map of name -> subschema, or a list of subschemas.
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")
_SCHEMA_SINGLE = ("items", "additionalItems", "additionalProperties", "not")


def has_keywords(schema: dict[str, Any], keywords: frozenset[str]) -> bool:
    """True if any schema node uses one of *keywords* (property names don't count)."""
    found = False

    def _visit(node: dict[str, Any]) -> None:
        nonlocal found
        if any(k in node for k in keywords):
            found = True

    _walk(schema, _visit)
    return found


def move_to_description(schema: dict[str, Any], keywords: frozenset[str]) -> dict[str, Any]:
    """Return a copy of *schema* with *keywords* removed and described instead."""
    schema = copy.deepcopy(schema)

    def _visit(node: dict[str, Any]) -> None:
        hints = []
        for key in _HINTS:
            if key in keywords and key in node:
                hints.append(_HINTS[key].format(node.pop(key)))
        if not hints:
            return
        hint = "; ".join(hints)
        description = node.get("description")
        if isinstance(description, str) and description.strip():
            node["description"] = f"{description} ({hint})"
        else:
            node["description"] = hint[0].upper() + hint[1:]

    _walk(schema, _visit)
    return schema


def _walk(node: Any, visit) -> None:
    """Call *visit* on every schema node, following only schema-valued keywords."""
    if not isinstance(node, dict):
        return
    visit(node)
    for key in _SCHEMA_MAPS:
        value = node.get(key)
        if isinstance(value, dict):
            for sub in value.values():
                _walk(sub, visit)
    for key in _SCHEMA_LISTS:
        value = node.get(key)
        if isinstance(value, list):
            for sub in value:
                _walk(sub, visit)
    for key in _SCHEMA_SINGLE:
        _walk(node.get(key), visit)
