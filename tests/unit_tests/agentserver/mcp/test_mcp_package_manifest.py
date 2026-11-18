# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for the manifest-driven MCP package format."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.mcp.package_manifest import (
    McpPackageError,
    iter_mcp_packages,
    load_mcp_package,
    resolve_mcp_package,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _manifest(package_id: str, integration: dict, **extra: object) -> dict:
    return {
        "version": "1.0.0",
        "package_type": "mcp",
        "id": package_id,
        "name": "Example MCP",
        "description": "Example MCP package",
        "display_name": {"zh": "示例 MCP", "en": "Example MCP"},
        "display_description": {"zh": "示例 MCP 包", "en": "Example MCP package"},
        "integration": integration,
        **extra,
    }


def test_loads_stdio_package_from_declared_manifest_paths(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest(
            "example",
            {"type": "stdio-mcp", "file": "config/server.json"},
            credentials={"type": "token", "file": "auth/schema.json"},
            icon="images/icon.png",
            skills=[{"dir": "skills/example", "mode": "all"}],
        ),
    )
    _write_json(
        package / "config/server.json",
        {"mcpServers": {"example": {"command": "npx", "args": ["example"]}}},
    )
    _write_json(package / "auth/schema.json", {"fields": []})
    (package / "images").mkdir()
    (package / "images/icon.png").write_bytes(b"PNG")
    (package / "skills/example").mkdir(parents=True)
    (package / "skills/example/SKILL.md").write_text("# Example", encoding="utf-8")

    loaded = load_mcp_package(package)

    assert loaded.package_id == "example"
    assert loaded.integration_type == "stdio-mcp"
    assert loaded.integration_file == package / "config/server.json"
    assert loaded.credentials_file == package / "auth/schema.json"
    assert loaded.icon_file == package / "images/icon.png"
    assert loaded.skill_dirs == (package / "skills/example",)
    assert loaded.server_name == "example"
    assert loaded.server_config["command"] == "npx"


@pytest.mark.parametrize(
    ("integration_type", "server_config"),
    [
        ("stdio-mcp", {"url": "https://example.test/mcp"}),
        ("remote-mcp", {"command": "npx"}),
    ],
)
def test_rejects_integration_type_that_disagrees_with_mcp_json(
    tmp_path: Path,
    integration_type: str,
    server_config: dict,
) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest("example", {"type": integration_type, "file": "mcp.json"}),
    )
    _write_json(package / "mcp.json", {"mcpServers": {"example": server_config}})

    with pytest.raises(McpPackageError, match=integration_type):
        load_mcp_package(package)


def test_cli_requires_declared_existing_cli_file(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest("example", {"type": "cli", "file": "cli.json"}),
    )

    with pytest.raises(McpPackageError, match="integration.file"):
        load_mcp_package(package)


def test_skill_only_without_skill_md_still_loads(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest(
            "example",
            {"type": "skill-only"},
            skills=[{"dir": "skills", "mode": "all"}],
        ),
    )
    (package / "skills").mkdir(parents=True)

    loaded = load_mcp_package(package)

    assert loaded.integration_type == "skill-only"
    assert loaded.skill_dirs == ()


def test_skill_dirs_without_skill_md_are_skipped(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest(
            "example",
            {"type": "cli", "file": "cli.json"},
            skills=[{"dir": "skills", "mode": "all"}],
        ),
    )
    _write_json(package / "cli.json", {})
    (package / "skills").mkdir(parents=True)

    loaded = load_mcp_package(package)

    assert loaded.skill_dirs == ()


@pytest.mark.parametrize("unsafe_path", ["../secret.json", "/tmp/secret.json"])
def test_rejects_paths_outside_package(tmp_path: Path, unsafe_path: str) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest("example", {"type": "cli", "file": unsafe_path}),
    )

    with pytest.raises(McpPackageError, match="inside the package"):
        load_mcp_package(package)


@pytest.mark.parametrize("package_id", ["different", "../example"])
def test_manifest_id_mismatch_uses_directory_name(
    tmp_path: Path, package_id: str
) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest(package_id, {"type": "cli", "file": "cli.json"}),
    )
    _write_json(package / "cli.json", {})

    loaded = load_mcp_package(package)

    assert loaded.package_id == "example"


def test_expected_id_is_still_enforced(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest("example", {"type": "cli", "file": "cli.json"}),
    )
    _write_json(package / "cli.json", {})

    with pytest.raises(McpPackageError, match="expected package id"):
        load_mcp_package(package, expected_id="other")


def test_missing_metadata_fields_fall_back(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        {"package_type": "mcp", "id": "example", "integration": {"type": "skill-only"}},
    )

    loaded = load_mcp_package(package)

    assert loaded.package_id == "example"
    assert loaded.name == "example"
    assert loaded.version == ""
    assert loaded.description == ""


@pytest.mark.parametrize(
    ("manifest_text", "expected"),
    [
        (None, "does not exist"),
        ("not json", "not valid JSON"),
        ('{"package_type":"plugin"}', "package_type"),
    ],
)
def test_rejects_missing_invalid_or_wrong_type_manifest(
    tmp_path: Path, manifest_text: str | None, expected: str
) -> None:
    package = tmp_path / "example"
    package.mkdir()
    if manifest_text is not None:
        (package / "manifest.json").write_text(manifest_text, encoding="utf-8")

    with pytest.raises(McpPackageError, match=expected):
        load_mcp_package(package)


@pytest.mark.parametrize(
    ("extra", "attribute"),
    [
        ({"credentials": {"type": "token", "file": "missing.json"}}, "credentials_file"),
        ({"icon": "missing.png"}, "icon_file"),
    ],
)
def test_missing_declared_component_is_ignored(
    tmp_path: Path, extra: dict, attribute: str
) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest("example", {"type": "cli", "file": "cli.json"}, **extra),
    )
    _write_json(package / "cli.json", {})

    loaded = load_mcp_package(package)

    assert getattr(loaded, attribute) is None


def test_non_png_icon_is_ignored(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest(
            "example",
            {"type": "cli", "file": "cli.json"},
            icon="icon.svg",
        ),
    )
    _write_json(package / "cli.json", {})
    (package / "icon.svg").write_text("<svg/>", encoding="utf-8")

    loaded = load_mcp_package(package)

    assert loaded.icon_file is None


@pytest.mark.parametrize(
    "display",
    [None, "纯字符串展示名", {"zh": "仅中文"}],
    ids=["absent", "plain-string", "zh-only"],
)
def test_display_fields_never_block_installation(
    tmp_path: Path, display: object, caplog: pytest.LogCaptureFixture
) -> None:
    package = tmp_path / "example"
    manifest = _manifest("example", {"type": "cli", "file": "cli.json"})
    for field in ("display_name", "display_description"):
        if display is None:
            manifest.pop(field)
        else:
            manifest[field] = display
    _write_json(package / "manifest.json", manifest)
    _write_json(package / "cli.json", {})

    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.common.mcp_package_manifest"):
        loaded = load_mcp_package(package)

    assert loaded.package_id == "example"
    assert loaded.integration_type == "cli"
    assert "display_name" in caplog.text


@pytest.mark.parametrize("credential_type", ["none", "cli_oauth", "unknown"])
def test_unsupported_declared_credential_type_degrades_to_none(
    tmp_path: Path, credential_type: str
) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest(
            "example",
            {"type": "cli", "file": "cli.json"},
            credentials={"type": credential_type},
        ),
    )
    _write_json(package / "cli.json", {})

    loaded = load_mcp_package(package)

    assert loaded.credentials_type == "none"


def test_rejects_symlinked_declared_path(tmp_path: Path) -> None:
    package = tmp_path / "example"
    outside = tmp_path / "outside.json"
    _write_json(outside, {})
    package.mkdir()
    (package / "cli.json").symlink_to(outside)
    _write_json(
        package / "manifest.json",
        _manifest("example", {"type": "cli", "file": "cli.json"}),
    )

    with pytest.raises(McpPackageError, match="symbolic link"):
        load_mcp_package(package)


def test_expected_id_validates_runtime_package_name_not_hub_uuid(tmp_path: Path) -> None:
    package = tmp_path / "example"
    _write_json(
        package / "manifest.json",
        _manifest("example", {"type": "cli", "file": "cli.json"}),
    )
    _write_json(package / "cli.json", {})

    assert load_mcp_package(package, expected_id="example").package_id == "example"
    with pytest.raises(McpPackageError, match="expected package id"):
        load_mcp_package(package, expected_id="hub-asset-uuid")


@pytest.mark.parametrize(
    ("package_id", "integration", "extra", "filename", "content"),
    [
        (
            "remote",
            {"type": "remote-mcp", "file": "mcp.json"},
            {},
            "mcp.json",
            {"mcpServers": {"remote": {"url": "https://example.test/mcp"}}},
        ),
        ("cli", {"type": "cli", "file": "cli.json"}, {}, "cli.json", {}),
        (
            "skill-only",
            {"type": "skill-only"},
            {"skills": [{"dir": "skills", "mode": "all"}]},
            "skills/SKILL.md",
            None,
        ),
    ],
)
def test_loads_each_supported_manifest_integration(
    tmp_path: Path,
    package_id: str,
    integration: dict,
    extra: dict,
    filename: str,
    content: dict | None,
) -> None:
    package = tmp_path / package_id
    _write_json(
        package / "manifest.json",
        _manifest(package_id, integration, **extra),
    )
    target = package / filename
    if content is None:
        target.parent.mkdir(parents=True)
        target.write_text("# Skill", encoding="utf-8")
    else:
        _write_json(target, content)

    assert load_mcp_package(package).integration_type == integration["type"]


def test_iterates_builtin_and_hub_roots_and_resolves_by_package_id(tmp_path: Path) -> None:
    builtin_root = tmp_path / "mcp_builtins"
    hub_root = tmp_path / "mcp_hub"
    for root, package_id in ((builtin_root, "builtin-one"), (hub_root, "hub-one")):
        package = root / package_id
        _write_json(
            package / "manifest.json",
            _manifest(package_id, {"type": "cli", "file": "cli.json"}),
        )
        _write_json(package / "cli.json", {})

    packages = iter_mcp_packages(builtin_root, hub_root)

    assert [package.package_id for package in packages] == ["builtin-one", "hub-one"]
    assert resolve_mcp_package("hub-one", builtin_root, hub_root).root == hub_root / "hub-one"
    assert resolve_mcp_package("missing", builtin_root, hub_root) is None


def test_duplicate_package_id_prefers_first_root(tmp_path: Path) -> None:
    """Resolve duplicate package IDs by root order, with built-ins first."""
    roots = (tmp_path / "mcp_builtins", tmp_path / "mcp_hub")
    for index, root in enumerate(roots):
        package = root / "duplicate"
        _write_json(
            package / "manifest.json",
            _manifest("duplicate", {"type": "cli", "file": f"cli-{index}.json"}),
        )
        _write_json(package / f"cli-{index}.json", {})

    packages = iter_mcp_packages(*roots)

    assert [package.package_id for package in packages] == ["duplicate"]
    assert resolve_mcp_package("duplicate", *roots).root == roots[0] / "duplicate"
