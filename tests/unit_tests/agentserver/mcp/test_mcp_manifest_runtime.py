# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime consumers must use MCP manifest declarations, not file probing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from jiuwenswarm.server.runtime.mcp import credential, package_manifest, registry


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_registry_reads_hub_root_and_ignores_undeclared_fixed_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "mcp" / "mcp_hub" / "hub-skill"
    _write_json(
        package / "manifest.json",
        {
            "version": "1.0.0",
            "package_type": "mcp",
            "id": "hub-skill",
            "name": "Hub Skill",
            "description": "Manifest-driven Hub package",
            "display_name": {"zh": "Hub 技能", "en": "Hub Skill"},
            "display_description": {"zh": "中文描述", "en": "English description"},
            "integration": {"type": "skill-only"},
            "skills": [{"dir": "skills", "mode": "all"}],
        },
    )
    (package / "skills").mkdir()
    (package / "skills/SKILL.md").write_text("# Hub", encoding="utf-8")
    _write_json(
        package / "mcp.json",
        {"mcpServers": {"stray": {"command": "must-not-be-used"}}},
    )
    _write_json(package / "token-schema.json", {"fields": [{"key": "SECRET", "required": True}]})
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)

    summary = next(item for item in registry.list_marketplace_mcps("builtin") if item["name"] == "hub-skill")

    assert summary["integration_type"] == "skill-only"
    assert summary["display_name"] == "Hub 技能"
    assert summary["source"] == "hub"
    assert credential.detect_credential_kind("hub-skill") == credential.KIND_NONE


def test_builtin_list_skips_invalid_package_without_hiding_valid_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "mcp" / "mcp_builtins"
    valid = root / "valid"
    _write_json(
        valid / "manifest.json",
        {
            "version": "1.0.0",
            "package_type": "mcp",
            "id": "valid",
            "name": "Valid",
            "description": "Valid package",
            "display_name": {"zh": "有效", "en": "Valid"},
            "display_description": {"zh": "有效包", "en": "Valid package"},
            "integration": {"type": "cli", "file": "cli.json"},
            "skills": [],
        },
    )
    _write_json(valid / "cli.json", {})
    _write_json(root / "broken" / "manifest.json", {"package_type": "mcp"})
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    warning = Mock()
    monkeypatch.setattr(package_manifest.logger, "warning", warning)

    items = registry.list_marketplace_mcps("builtin")

    assert [item["name"] for item in items] == ["valid"]
    assert items[0]["source"] == "built_in"
    warning.assert_called_once()
    assert "broken" in str(warning.call_args)
