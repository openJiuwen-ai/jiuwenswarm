# coding: utf-8
"""Compatibility fixes for OpenJiuwen MCP JSON Schema-to-Pydantic conversion."""

from __future__ import annotations

from typing import Any

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate
from openjiuwen.core.common.logging import logger

_PATCHED = False

__all__ = ["apply_openjiuwen_schema_compat_patch"]


def apply_openjiuwen_schema_compat_patch() -> None:
    """Translate removed JSON Schema terms before Pydantic builds MCP models.

    Pydantic 2.13 removed ``Field(unique_items=...)``. OpenJiuwen 0.1.19 still
    forwards JSON Schema's ``uniqueItems`` to that keyword, so schemas such as
    REA's ``inspect_managed_members`` fail during argument formatting with
    OpenJiuwen error 189002 before the MCP tool executes.

    ``SchemaUtils.format_with_schema`` validates against the original JSON
    Schema before building its temporary Pydantic model. Removing the keyword
    only from that model-construction step preserves the original
    ``uniqueItems`` validation while avoiding the removed Pydantic API.
    """
    global _PATCHED
    if _PATCHED:
        return

    from openjiuwen.core.common.utils.schema_utils import SchemaUtils
    from openjiuwen.core.common.exception.codes import StatusCode
    from openjiuwen.core.common.exception.errors import build_error

    original = SchemaUtils._convert_schema_to_field  # pylint: disable=protected-access
    original_validate = SchemaUtils.validate_with_schema

    def _convert_schema_to_field(
        schema: dict[str, Any],
        required: bool = False,
        alias: str | None = None,
        serialization_alias: str | None = None,
    ) -> Any:
        if isinstance(schema, dict) and "uniqueItems" in schema:
            schema = {
                key: value
                for key, value in schema.items()
                if key != "uniqueItems"
            }
        return original(
            schema,
            required=required,
            alias=alias,
            serialization_alias=serialization_alias,
        )

    def _schema_has_unique_items(schema: Any) -> bool:
        if isinstance(schema, dict):
            if "uniqueItems" in schema:
                return True
            return any(_schema_has_unique_items(value) for value in schema.values())
        if isinstance(schema, list):
            return any(_schema_has_unique_items(value) for value in schema)
        return False

    def _validate_with_schema(data: Any, schema: Any) -> None:
        # OpenJiuwen falls back to its generated Pydantic model when JSON Schema
        # validation fails. After removing unique_items from that model, that
        # fallback would silently accept duplicate array entries. Keep the
        # authoritative JSON Schema error for uniqueItems-bearing schemas.
        if isinstance(schema, dict) and _schema_has_unique_items(schema):
            try:
                jsonschema_validate(data, schema)
            except JsonSchemaValidationError as exc:
                raise build_error(
                    StatusCode.SCHEMA_VALIDATE_INVALID,
                    cause=exc,
                    reason=str(exc),
                    data=data,
                ) from exc
        return original_validate(data, schema)

    setattr(
        SchemaUtils,
        "_convert_schema_to_field",
        staticmethod(_convert_schema_to_field),
    )
    setattr(
        SchemaUtils,
        "validate_with_schema",
        staticmethod(_validate_with_schema),
    )
    _PATCHED = True
    logger.info("[schema-compat] removed Pydantic 2.13-incompatible unique_items keyword")
