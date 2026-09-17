# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway MCP 模板 transport 别名与 SDK 对齐（TC_MCP_CALL_005）。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _import_template_schemas() -> Any:
    parent_name = "jiuwenswarm.loaded_extension"
    package_name = f"{parent_name}.manager_config_receiver"
    extension_root = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "jiuwenclaw-ee"
        / "gateway"
        / "extensions"
        / "manager_config_receiver"
    )
    if parent_name not in sys.modules:
        parent = ModuleType(parent_name)
        parent.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent_name] = parent
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(extension_root)]  # type: ignore[attr-defined]
        package.__package__ = package_name
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.schemas.template_schemas")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http", "streamable-http"),
        ("HTTP", "streamable-http"),
        ("streamable_http", "streamable-http"),
        ("streamable-http", "streamable-http"),
        ("sse", "sse"),
    ],
)
def test_validate_mcp_entry_normalizes_http_alias(raw: str, expected: str) -> None:
    validate_mcp_entry = _import_template_schemas().validate_mcp_entry
    out = validate_mcp_entry(
        {
            "name": "qa-streamable-http",
            "transport": raw,
            "url": "http://192.168.1.96:18016/mcp",
            "timeout_s": 10,
        }
    )
    assert out["transport"] == expected
    assert out["name"] == "qa-streamable-http"


def test_validate_mcp_entry_rejects_unknown_transport() -> None:
    validate_mcp_entry = _import_template_schemas().validate_mcp_entry
    with pytest.raises(ValueError, match="mcp_entry.transport must be one of"):
        validate_mcp_entry(
            {
                "name": "bad",
                "transport": "websocket",
                "url": "http://example.com/mcp",
            }
        )


def test_validate_mcp_entry_rejects_stdio() -> None:
    validate_mcp_entry = _import_template_schemas().validate_mcp_entry
    with pytest.raises(ValueError, match="mcp_entry.transport must be one of"):
        validate_mcp_entry(
            {
                "name": "local",
                "transport": "stdio",
                "command": "node",
                "args": ["server.js"],
            }
        )
