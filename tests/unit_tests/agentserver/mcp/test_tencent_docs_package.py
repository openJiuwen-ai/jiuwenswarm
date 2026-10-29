# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for the shipped tencent-docs MCP connector package.

Validates the real artifact under ``connectors/tencent-docs/`` against the
manifest contract (load_mcp_package), the official Tencent Docs MCP endpoint
and token-schema, and credential placeholder injection via CredentialStore.
"""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.server.runtime.mcp.credential import (
    CredentialStore,
    resolve_placeholders,
)
from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package

PACKAGE_DIR = Path(__file__).resolve().parents[4] / "connectors" / "tencent-docs"
OFFICIAL_MCP_URL = "https://docs.qq.com/openapi/mcp"
OFFICIAL_AUTH_URL = "https://docs.qq.com/open/document/mcp/get-token/"
TOKEN_KEY = "TENCENT_DOCS_TOKEN"


def test_package_loads_and_declares_remote_mcp_integration() -> None:
    loaded = load_mcp_package(PACKAGE_DIR)

    assert loaded.package_id == "tencent-docs"
    assert loaded.integration_type == "remote-mcp"
    assert loaded.server_name == "tencent-docs"


def test_mcp_json_points_at_official_endpoint_with_single_server() -> None:
    loaded = load_mcp_package(PACKAGE_DIR)

    assert loaded.server_config["url"] == OFFICIAL_MCP_URL
    assert loaded.server_config["timeout"] == 600
    raw = json.loads((PACKAGE_DIR / "mcp.json").read_text(encoding="utf-8"))
    assert list(raw["mcpServers"]) == ["tencent-docs"]
    headers = raw["mcpServers"]["tencent-docs"]["headers"]
    assert headers["Authorization"] == f"${{{TOKEN_KEY}}}"


def test_manifest_bilingual_fields_category_and_examples() -> None:
    manifest = json.loads((PACKAGE_DIR / "manifest.json").read_text(encoding="utf-8"))

    for field in ("display_name", "display_description"):
        assert set(manifest[field]) == {"zh", "en"}
    assert manifest["category"] == "办公与生产力"
    assert manifest["examples"]["zh"] and manifest["examples"]["en"]


def test_token_schema_matches_authorization_placeholder() -> None:
    loaded = load_mcp_package(PACKAGE_DIR)

    schema = json.loads(loaded.credentials_file.read_text(encoding="utf-8"))
    keys = [field["key"] for field in schema["fields"]]
    assert keys == [TOKEN_KEY]
    assert schema["fields"][0]["required"] is True
    assert schema["fields"][0]["type"] == "password"
    assert schema["docUrl"] == OFFICIAL_AUTH_URL
    header_template = loaded.server_config["headers"]["Authorization"]
    assert f"${{{TOKEN_KEY}}}" in header_template


def test_icon_is_real_png() -> None:
    loaded = load_mcp_package(PACKAGE_DIR)

    assert loaded.icon_file.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_guide_skill_present_with_write_confirmation_discipline() -> None:
    loaded = load_mcp_package(PACKAGE_DIR)

    assert len(loaded.skill_dirs) == 1
    text = (loaded.skill_dirs[0] / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: tencent-docs-guide" in text
    assert "写操作两阶段确认纪律" in text


def test_authorization_placeholder_resolves_from_credential_store(tmp_path: Path) -> None:
    store = CredentialStore(workspace_dir=tmp_path)
    store.save_token("tencent-docs", TOKEN_KEY, "real-token-xyz")

    raw = json.loads((PACKAGE_DIR / "mcp.json").read_text(encoding="utf-8"))
    server = raw["mcpServers"]["tencent-docs"]
    entry = {
        "name": "tencent-docs",
        "url": server["url"],
        "headers": dict(server["headers"]),
    }
    resolved = resolve_placeholders(entry, store)

    assert resolved["headers"]["Authorization"] == "real-token-xyz"


def test_missing_token_leaves_placeholder_literal(tmp_path: Path) -> None:
    store = CredentialStore(workspace_dir=tmp_path)
    entry = {"name": "tencent-docs", "headers": {"Authorization": f"${{{TOKEN_KEY}}}"}}

    resolved = resolve_placeholders(entry, store)

    assert resolved["headers"]["Authorization"] == f"${{{TOKEN_KEY}}}"
