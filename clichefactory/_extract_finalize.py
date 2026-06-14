"""Shared finalize step: coerce → postprocess → validate or partial envelope."""
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from clichefactory._utils import _coerce_numeric_strings
from clichefactory.types import PartialExtraction, PostprocessFn

T = TypeVar("T", bound=BaseModel)


def finalize_extract_result(
    result: dict[str, Any],
    schema: type[T] | dict[str, Any] | None,
    postprocess: PostprocessFn | None,
    *,
    allow_partial: bool,
    validation_errors: list[dict[str, Any]] | None = None,
    response_status: str | None = None,
) -> T | PartialExtraction | dict[str, Any]:
    """Coerce, postprocess, then return ``PartialExtraction`` or validated model instance.

    When ``schema is None`` (schemaless / Shape B — the schema lives in the
    referenced ``config_id``), there is no client-side model to validate
    against, so the coerced result dict is returned as-is.
    """
    result = _coerce_numeric_strings(result)
    if postprocess is not None:
        result = postprocess(result)

    if allow_partial and (
        response_status == "partial" or (validation_errors is not None and len(validation_errors) > 0)
    ):
        return PartialExtraction(
            raw=result,
            validation_errors=list(validation_errors or []),
        )

    if schema is None:
        return result

    if isinstance(schema, dict):
        from clichefactory._schema import canonical_schema_to_pydantic

        model_cls = canonical_schema_to_pydantic(schema)
        return model_cls.model_validate(result)
    return schema.model_validate(result)
