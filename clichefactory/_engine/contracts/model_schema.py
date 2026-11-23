"""Kept so this import path still works; the implementation is ``clichefactory._schema``."""
from clichefactory._schema import (  # noqa: F401
    batch_config_to_canonical,
    canonical_schema_to_pydantic,
    simple_schema_to_canonical,
    validate_model_schema,
)
