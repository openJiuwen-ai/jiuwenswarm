# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jiuwenswarm.common import playwright_mcp_runtime as runtime


def _make_bundle(
    root: Path,
    *,
    members: dict[str, bytes] | None = None,
    archive_hash: str | None = None,
) -> Path:
    resources = root / "resources" / "runtime" / "playwright-mcp"
    resources.mkdir(parents=True)
    archive = resources / "playwright-mcp-0.0.78.zip"
    payload = members or {
        "node_modules/@playwright/mcp/cli.js": b"console.log('test');\n",
        "node_modules/@playwright/mcp/package.json": b'{"version":"0.0.78"}\n',
    }
    with zipfile.ZipFile(archive, "w") as output:
        for name, content in payload.items():
            output.writestr(name, content)
    actual_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "package": "@playwright/mcp",
        "version": runtime.PLAYWRIGHT_MCP_VERSION,
        "minimum_node_version": runtime.MINIMUM_NODE_VERSION,
        "cli_relative_path": "node_modules/@playwright/mcp/cli.js",
        "archive": archive.name,
        "archive_sha256": archive_hash or actual_hash,
        "provenance": {},
    }
    (resources / runtime.MANIFEST_FILENAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return resources


def _node_result(version: str = "v22.11.0", returncode: int = 0):
    def run(*args, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=version, stderr="")

    return run


def test_materialize_verifies_extracts_and_reuses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _make_bundle(tmp_path)
    workspace = tmp_path / "user data with spaces"
    first_cli, first_manifest = runtime.materialize_bundled_runtime(
        resource_dir=resources,
        user_workspace_dir=workspace,
    )
    assert first_cli.read_text(encoding="utf-8") == "console.log('test');\n"
    assert first_manifest["version"] == "0.0.78"
    assert json.loads((first_cli.parents[3] / ".complete.json").read_text())["archive_sha256"]

    monkeypatch.setattr(
        runtime,
        "_extract_archive_safely",
        lambda *args, **kwargs: pytest.fail("valid cache should not be extracted again"),
    )
    second_cli, _ = runtime.materialize_bundled_runtime(
        resource_dir=resources,
        user_workspace_dir=workspace,
    )
    assert second_cli == first_cli


def test_materialize_rejects_corrupt_and_traversing_archives(tmp_path: Path) -> None:
    corrupt = _make_bundle(tmp_path / "corrupt", archive_hash="0" * 64)
    with pytest.raises(runtime.PlaywrightMcpRuntimeError, match="hash mismatch"):
        runtime.materialize_bundled_runtime(
            resource_dir=corrupt,
            user_workspace_dir=tmp_path / "corrupt-cache",
        )

    traversing = _make_bundle(
        tmp_path / "traversing",
        members={
            "../escape.js": b"bad",
            "node_modules/@playwright/mcp/cli.js": b"good",
        },
    )
    with pytest.raises(runtime.PlaywrightMcpRuntimeError, match="safe relative path"):
        runtime.materialize_bundled_runtime(
            resource_dir=traversing,
            user_workspace_dir=tmp_path / "traversing-cache",
        )
    assert not (tmp_path / "escape.js").exists()


def test_concurrent_materialization_never_exposes_partial_cache(tmp_path: Path) -> None:
    resources = _make_bundle(tmp_path)
    workspace = tmp_path / "shared-cache"

    def materialize() -> Path:
        return runtime.materialize_bundled_runtime(
            resource_dir=resources,
            user_workspace_dir=workspace,
        )[0]

    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(lambda _: materialize(), range(8)))
    assert len(set(paths)) == 1
    assert paths[0].is_file()
    version_root = workspace / "runtime" / "playwright-mcp" / "0.0.78"
    assert not list(version_root.glob(".*.tmp-*"))


@pytest.mark.parametrize(
    ("version", "expected"),
    [("v20.0.0", True), ("v22.11.0", True), ("v19.9.0", False), ("unknown", False)],
)
def test_node_version_requirement(tmp_path: Path, version: str, expected: bool) -> None:
    node = tmp_path / "Node Runtime" / "node.exe"
    if expected:
        assert runtime.resolve_node_executable(
            which=lambda _: str(node), run=_node_result(version)
        ) == str(node.resolve())
    else:
        with pytest.raises(runtime.PlaywrightMcpRuntimeError):
            runtime.resolve_node_executable(
                which=lambda _: str(node), run=_node_result(version)
            )


def test_launch_precedence_override_bundle_and_exact_fallback(tmp_path: Path) -> None:
    command_only = runtime.resolve_playwright_mcp_launch(
        environ={"PLAYWRIGHT_MCP_COMMAND": "/opt/node"},
    )
    assert command_only.source == "override"
    assert command_only.command == "/opt/node"
    assert command_only.args == ("-y", "@playwright/mcp@0.0.78")

    args_only = runtime.resolve_playwright_mcp_launch(
        environ={"PLAYWRIGHT_MCP_ARGS": '["C:/path with spaces/cli.js", "--headless"]'},
    )
    assert args_only.source == "override"
    assert args_only.command == "npx"
    assert args_only.args[0] == "C:/path with spaces/cli.js"

    resources = _make_bundle(tmp_path / "bundle")
    bundled = runtime.resolve_playwright_mcp_launch(
        environ={},
        resource_dir=resources,
        user_workspace_dir=tmp_path / "workspace with spaces",
        which=lambda _: str(tmp_path / "Node Runtime" / "node.exe"),
        run=_node_result(),
    )
    assert bundled.source == "bundled"
    assert bundled.command.endswith("node.exe")
    assert len(bundled.args) == 1
    assert "workspace with spaces" in bundled.args[0]

    fallback = runtime.resolve_playwright_mcp_launch(
        environ={},
        resource_dir=tmp_path / "missing",
        user_workspace_dir=tmp_path / "fallback-cache",
    )
    assert fallback.source == "pinned-npx-fallback"
    assert fallback.command == "npx"
    assert fallback.args == ("-y", "@playwright/mcp@0.0.78")


def test_legacy_template_defaults_select_bundle_without_rewriting_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _make_bundle(tmp_path / "bundle")
    warnings: list[str] = []
    monkeypatch.setattr(
        runtime.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    legacy_environment = {
        "PLAYWRIGHT_MCP_COMMAND": "npx",
        "PLAYWRIGHT_MCP_ARGS": "-y @playwright/mcp@latest",
    }

    launch = runtime.resolve_playwright_mcp_launch(
        environ=legacy_environment,
        resource_dir=resources,
        user_workspace_dir=tmp_path / "workspace",
        which=lambda _: str(tmp_path / "Node Runtime" / "node.exe"),
        run=_node_result(),
    )

    assert launch.source == "bundled"
    assert launch.command.endswith("node.exe")
    assert legacy_environment == {
        "PLAYWRIGHT_MCP_COMMAND": "npx",
        "PLAYWRIGHT_MCP_ARGS": "-y @playwright/mcp@latest",
    }
    assert any("Ignoring legacy Playwright MCP .env defaults" in item for item in warnings)


@pytest.mark.parametrize(
    "environment",
    [
        {
            "PLAYWRIGHT_MCP_COMMAND": "custom-npx",
            "PLAYWRIGHT_MCP_ARGS": "-y @playwright/mcp@latest",
        },
        {
            "PLAYWRIGHT_MCP_COMMAND": "npx",
            "PLAYWRIGHT_MCP_ARGS": "-y @playwright/mcp@0.0.78",
        },
        {"PLAYWRIGHT_MCP_COMMAND": "npx"},
        {"PLAYWRIGHT_MCP_ARGS": "-y @playwright/mcp@latest"},
    ],
)
def test_nonlegacy_playwright_settings_remain_overrides(
    environment: dict[str, str],
) -> None:
    assert runtime._environment_has_override(environment) is True


def test_managed_environment_is_not_reclassified_as_override() -> None:
    launch = runtime.PlaywrightMcpLaunch(
        source="bundled",
        command="C:/Node Runtime/node.exe",
        args=("C:/MCP Runtime/cli.js",),
        version="0.0.78",
    )
    args_json = runtime.serialize_playwright_mcp_args(launch.args)
    environ: dict[str, str] = {
        "PLAYWRIGHT_MCP_COMMAND": launch.command,
        "PLAYWRIGHT_MCP_ARGS": args_json,
    }
    runtime.record_managed_launch_environment(environ, launch, args_json)
    assert runtime._environment_has_override(environ) is False
    environ["PLAYWRIGHT_MCP_COMMAND"] = "custom-node"
    assert runtime._environment_has_override(environ) is True


def test_clear_managed_launch_environment_preserves_administrator_overrides() -> None:
    bundled = runtime.PlaywrightMcpLaunch(
        source="bundled",
        command="C:/Node Runtime/node.exe",
        args=("C:/MCP Runtime/cli.js",),
        version="0.0.78",
    )
    bundled_args = runtime.serialize_playwright_mcp_args(bundled.args)
    managed: dict[str, str] = {
        "PLAYWRIGHT_MCP_COMMAND": bundled.command,
        "PLAYWRIGHT_MCP_ARGS": bundled_args,
    }
    runtime.record_managed_launch_environment(managed, bundled, bundled_args)

    runtime.clear_managed_launch_environment(managed)

    assert managed == {}

    override = runtime.PlaywrightMcpLaunch(
        source="override",
        command="custom-node",
        args=("custom-cli.js",),
        version="administrator-override",
    )
    override_args = runtime.serialize_playwright_mcp_args(override.args)
    administrator_environment = {
        "PLAYWRIGHT_MCP_COMMAND": override.command,
        "PLAYWRIGHT_MCP_ARGS": override_args,
    }
    runtime.record_managed_launch_environment(
        administrator_environment,
        override,
        override_args,
    )

    runtime.clear_managed_launch_environment(administrator_environment)

    assert administrator_environment == {
        "PLAYWRIGHT_MCP_COMMAND": "custom-node",
        "PLAYWRIGHT_MCP_ARGS": override_args,
    }


def test_committed_artifact_matches_manifest_and_contains_no_browser_binary() -> None:
    resources = (
        Path(runtime.__file__).resolve().parents[1]
        / "resources"
        / "runtime"
        / "playwright-mcp"
    )
    manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
    archive = resources / manifest["archive"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    assert "--bin-links=false" in manifest["provenance"]["install_command"]
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    assert manifest["cli_relative_path"] in names
    assert "node_modules/.package-lock.json" not in names
    assert all("/node_modules/.bin/" not in f"/{name}" for name in names)
    # PowerShell sources used by the runtime are portable archive inputs; only
    # npm-generated .bin shims are excluded.
    assert "node_modules/playwright-core/bin/install_media_pack.ps1" in names
    assert all(".local-browsers" not in name.lower() for name in names)
    assert all(not name.lower().endswith(("/chrome.exe", "/headless_shell", "/firefox.exe")) for name in names)
