# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime consumers must use MCP manifest declarations, not file probing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from jiuwenswarm.common import mcp_package_manifest as package_manifest
from jiuwenswarm.server.runtime.mcp import credential, registry


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
    assert any("broken" in str(call) for call in warning.call_args_list)


def test_missing_display_fields_fall_back_to_name_and_description(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "mcp" / "mcp_hub" / "arxiv-zh-mcp"
    _write_json(
        package / "manifest.json",
        {
            "version": "1.0.0",
            "package_type": "mcp",
            "id": "arxiv-zh-mcp",
            "name": "arXiv 中文化",
            "description": "Translate arXiv LaTeX sources into scholarly Chinese",
            "integration": {"type": "stdio-mcp", "file": "mcp.json"},
            "credentials": {"type": "token", "file": "token-schema.json"},
        },
    )
    _write_json(
        package / "mcp.json",
        {"mcpServers": {"arxiv-zh-mcp": {"command": "uvx", "args": ["arxiv-zh-mcp"]}}},
    )
    _write_json(package / "token-schema.json", {"fields": []})
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)

    summary = next(
        item
        for item in registry.list_marketplace_mcps("builtin")
        if item["name"] == "arxiv-zh-mcp"
    )

    assert summary["display_name"] == "arXiv 中文化"
    assert summary["description"] == "Translate arXiv LaTeX sources into scholarly Chinese"


def test_oauth_credential_kind_is_supported(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "mcp" / "mcp_hub" / "canva"
    _write_json(
        package / "manifest.json",
        {
            "version": "1.0.0",
            "package_type": "mcp",
            "id": "canva",
            "name": "Canva",
            "description": "Canva MCP",
            "display_name": {"zh": "Canva 可画", "en": "Canva"},
            "display_description": {"zh": "Canva 中国 MCP", "en": "Canva China MCP"},
            "integration": {"type": "remote-mcp", "file": "mcp.json"},
            "credentials": {"type": "oauth"},
            "skills": [],
        },
    )
    _write_json(
        package / "mcp.json",
        {"mcpServers": {"canva": {"url": "https://mcp.canva.cn/mcp"}}},
    )
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)

    assert credential.detect_credential_kind("canva") == credential.KIND_OAUTH


def test_list_parses_each_package_manifest_once(tmp_path: Path, monkeypatch) -> None:
    """One list call resolves every package exactly once (no per-field reloads)."""
    root = tmp_path / "mcp" / "mcp_builtins"
    for name in ("alpha", "beta"):
        package = root / name
        package.mkdir(parents=True)
        _write_json(
            package / "manifest.json",
            {
                "version": "1.0.0",
                "package_type": "mcp",
                "id": name,
                "name": name,
                "description": f"{name} package",
                "integration": {"type": "skill-only"},
                "skills": [],
            },
        )
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)

    loads: list[str] = []
    real_load = package_manifest.load_mcp_package

    def counting_load(path, *args, **kwargs):
        loads.append(Path(path).name)
        return real_load(path, *args, **kwargs)

    monkeypatch.setattr(package_manifest, "load_mcp_package", counting_load)

    items = registry.list_marketplace_mcps("builtin")

    assert [item["name"] for item in items] == ["alpha", "beta"]
    assert loads == ["alpha", "beta"]


def test_list_returns_signed_icon_urls_instead_of_inline_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """List payloads carry a short URL; the bytes are served by the download API."""
    from urllib.parse import parse_qs, urlsplit

    from jiuwenswarm.common.file_download_token import WebFileDownloadManager
    from jiuwenswarm.server.runtime import package_avatars
    from tests.unit_tests.agentserver.mcp.manifest_helpers import (
        write_canva_oauth_package,
    )

    package = write_canva_oauth_package(tmp_path / "mcp" / "mcp_builtins" / "canva")
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(package_avatars, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        package_avatars,
        "get_package_avatar_cache_dir",
        lambda: tmp_path / ".cache" / "package-avatars",
    )
    monkeypatch.setattr(
        WebFileDownloadManager,
        "_instance",
        WebFileDownloadManager(secret="mcp-icon-test-secret"),
    )
    package_avatars._AVATAR_URL_CACHE.clear()

    summary = next(
        item for item in registry.list_marketplace_mcps("builtin")
        if item["name"] == "canva"
    )

    icon = summary["icon"]
    assert isinstance(icon, str)
    assert icon.startswith("/file-api/download?token=")
    assert icon.endswith("&inline=1")
    token = parse_qs(urlsplit(icon).query)["token"][0]
    claims = WebFileDownloadManager.get_instance().validate_token(token)
    assert claims is not None
    assert Path(claims["path"]).read_bytes() == (package / "icon.png").read_bytes()
