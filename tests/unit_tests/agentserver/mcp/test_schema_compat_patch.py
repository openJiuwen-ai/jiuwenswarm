"""Regression tests for Pydantic 2.13 MCP schema compatibility."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.mcp.schema_compat_patch import (
    apply_openjiuwen_schema_compat_patch,
)
from openjiuwen.core.common.utils.schema_utils import SchemaUtils


def test_unique_items_schema_formats_without_removed_pydantic_keyword() -> None:
    apply_openjiuwen_schema_compat_patch()
    schema = {
        "type": "object",
        "required": ["type_names"],
        "properties": {
            "type_names": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            }
        },
    }

    assert SchemaUtils.format_with_schema(
        {"type_names": ["method", "class"]},
        schema,
    ) == {"type_names": ["method", "class"]}


def test_unique_items_validation_is_preserved() -> None:
    apply_openjiuwen_schema_compat_patch()
    schema = {
        "type": "object",
        "required": ["type_names"],
        "properties": {
            "type_names": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            }
        },
    }

    with pytest.raises(Exception, match="(?i)unique|not unique"):
        SchemaUtils.format_with_schema(
            {"type_names": ["method", "method"]},
            schema,
        )
