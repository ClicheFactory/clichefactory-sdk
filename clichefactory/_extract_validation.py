"""Shared Pydantic validation for LLM extraction JSON (fast path + providers).

Kept outside ``ai_clients`` so importing validation helpers does not load Gemini/OpenAI/etc.
"""
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError as PydanticValidationError

T = TypeVar("T", bound=BaseModel)


class RawExtractionValidationError(Exception):
    """JSON parsed to a dict but ``model_validate`` failed.

    Used when the caller needs the invalid payload (e.g. service ``allow_partial``).
    The original :class:`pydantic.ValidationError` is chained as ``__cause__``.
    """

    def __init__(self, data: dict[str, Any], validation_errors: list[dict[str, Any]]) -> None:
        self.data = data
        self.validation_errors = validation_errors
        super().__init__(str(validation_errors))


def validate_extracted_dict(schema: type[T], data: dict[str, Any]) -> T:
    try:
        return schema.model_validate(data)
    except PydanticValidationError as e:
        raise RawExtractionValidationError(
            data=data,
            validation_errors=e.errors(),
        ) from e


def validate_or_raise_raw(
    schema: type[T],
    data: dict[str, Any],
    *,
    raise_on_validation_error: bool = True,
) -> T:
    """Validate extracted dict; by default re-raise plain :class:`pydantic.ValidationError`."""
    try:
        return validate_extracted_dict(schema, data)
    except RawExtractionValidationError as e:
        if raise_on_validation_error:
            raise e.__cause__ from e
        raise e
