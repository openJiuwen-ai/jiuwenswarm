# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for Hub MCP connect on node-only hosts (issue #5510)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.mcp_config import (
    resolve_node_package_runner,
    build_mcp_server_config,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetSummary,
    HubSearchPage,
)
from jiuwenswarm.server.runtime.mcp.marketplace import (
    _resolve_hub_asset_id_by_package_name,
)
from jiuwenswarm.server.runtime.mcp.registry import (
    CODE_CLI_INCOMPLETE,
    CODE_RUNTIME_MISSING,
    _classify_install_failure,
)


def _fake_which(mapping: dict[str, str | None]):
    def which(name: str) -> str | None:
        return mapping.get(name)
    return which


def test_npx_falls_back_to_corepack_when_launcher_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_config.shutil, "which", _fake_which({"corepack": "/usr/bin/corepack"})
    )
    assert resolve_node_package_runner("npx", ["-y", "@amap/amap-maps-mcp-server"]) == (
        "corepack",
        ["npx", "-y", "@amap/amap-maps-mcp-server"],
    )


def test_launcher_kept_when_present(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_config.shutil,
        "which",
        _fake_which({"npx": "/usr/bin/npx", "corepack": "/usr/bin/corepack"}),
    )
    assert resolve_node_package_runner("npx", ["-y", "pkg"]) == ("npx", ["-y", "pkg"])


def test_launcher_kept_when_corepack_missing(monkeypatch) -> None:
    monkeypatch.setattr(mcp_config.shutil, "which", _fake_which({}))
    assert resolve_node_package_runner("npx", ["pkg"]) == ("npx", ["pkg"])


def test_non_node_launcher_untouched(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_config.shutil, "which", _fake_which({"corepack": "/usr/bin/corepack"})
    )
    assert resolve_node_package_runner("uvx", ["pkg"]) == ("uvx", ["pkg"])


def test_build_config_rewrites_launcher_only_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_config.shutil, "which", _fake_which({"corepack": "/usr/bin/corepack"})
    )
    entry = {"name": "amap", "transport": "stdio", "command": "npx", "args": ["-y", "pkg"]}
    plain = build_mcp_server_config(entry)
    assert plain is not None and plain.params["command"] == "npx"
    resolved = build_mcp_server_config(entry, resolve_node_launcher=True)
    assert resolved is not None
    assert resolved.params["command"] == "corepack"
    assert resolved.params["args"] == ["npx", "-y", "pkg"]


def _summary(asset_id: str, package_name: str) -> HubAssetSummary:
    return HubAssetSummary(
        kind="mcp",
        asset_id=asset_id,
        display_name=package_name,
        short_description="",
        public_latest_version="1.0.0",
        icon_uri="",
        tags=(),
        package_name=package_name,
    )


class _FakeHubPort:
    def __init__(self, items: list[HubAssetSummary]) -> None:
        self.items = items

    async def search_assets(self, request):
        return HubSearchPage(
            items=tuple(self.items), total=len(self.items), page=1, page_size=100
        )


@pytest.mark.asyncio
async def test_resolve_hub_asset_id_requires_exact_package_name() -> None:
    port = _FakeHubPort([_summary("fuzzy-id", "amap-extra"), _summary("amap-id", "amap")])
    assert await _resolve_hub_asset_id_by_package_name(port, "amap") == "amap-id"


@pytest.mark.asyncio
async def test_resolve_hub_asset_id_rejects_fuzzy_only_hit() -> None:
    port = _FakeHubPort([_summary("fuzzy-id", "amap-extra")])
    assert await _resolve_hub_asset_id_by_package_name(port, "amap") == ""


def _install_result(**overrides):
    fields = {
        "runtime": "node",
        "install_cmd": "npm install -g dingtalk-workspace-cli@1.0.61",
        "error_kind": "binary_not_found",
        "error": "version command not found",
        "cli_binary_missing": True,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_cli_binary_missing_maps_to_cli_incomplete() -> None:
    err = _classify_install_failure("dingtalk", _install_result())
    assert err.code == CODE_CLI_INCOMPLETE
    assert err.install_cmd == "npm install -g dingtalk-workspace-cli@1.0.61"


def test_runtime_binary_missing_still_maps_to_runtime_missing() -> None:
    err = _classify_install_failure("dingtalk", _install_result(cli_binary_missing=False))
    assert err.code == CODE_RUNTIME_MISSING