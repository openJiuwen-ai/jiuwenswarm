# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the build-time OfficeClaw MCP manifest fast path.

When ``OFFICE_CLAW_MCP_MANIFEST_PATH`` points at a manifest whose bundle
fingerprint matches, ``list_office_claw_mcp_tools`` must return schemas from
the manifest without spawning the MCP server (TTFT critical-path optimization).
A fingerprint mismatch, missing file, or missing env falls back to live
discovery.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.mcp_config import (
    _load_office_claw_mcp_manifest,
    list_office_claw_mcp_tools,
)


def _write_bundle(tmp_path: Path, content: str = "bundle v1") -> Path:
    bundle = tmp_path / "index.js"
    bundle.write_text(content, encoding="utf-8")
    return bundle


def _bundle_fingerprint(bundle: Path) -> list[dict[str, object]]:
    import hashlib
    content = bundle.read_bytes()
    return [{"path": str(bundle), "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]


def _write_manifest(
    tmp_path: Path,
    tools: list[dict[str, object]],
    build_files: list[dict[str, object]],
) -> Path:
    manifest = tmp_path / "office-claw-tool-manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "buildFiles": build_files, "tools": tools}, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _params(bundle: Path, *, excluded: str = "") -> dict[str, object]:
    env: dict[str, object] = {}
    if excluded:
        env["OFFICE_CLAW_MCP_EXCLUDED_TOOLS"] = excluded
    return {"command": "node", "args": [str(bundle)], "cwd": str(bundle.parent), "env": env}


def _tools() -> list[dict[str, object]]:
    return [
        {"name": "office_claw_post_message", "description": "post", "input_params": {"type": "object"}},
        {"name": "office_claw_list_tasks", "description": "list", "input_params": {"type": "object"}},
        {"name": "limb_tool_guide", "description": "guide", "input_params": {"type": "object"}},
    ]


@pytest.fixture(autouse=True)
def _clear_schema_cache() -> None:
    mcp_config.invalidate_office_claw_mcp_schema_cache()
    yield
    mcp_config.invalidate_office_claw_mcp_schema_cache()


@pytest.mark.asyncio
async def test_manifest_hit_returns_tools_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    manifest = _write_manifest(tmp_path, _tools(), _bundle_fingerprint(bundle))
    monkeypatch.setenv("OFFICE_CLAW_MCP_MANIFEST_PATH", str(manifest))
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(side_effect=AssertionError("spawn should be skipped")),
    )

    result = await list_office_claw_mcp_tools(_params(bundle))

    names = [t["name"] for t in result]
    assert names == ["office_claw_post_message", "office_claw_list_tasks", "limb_tool_guide"]
    assert result[0]["input_params"] == {"type": "object"}


@pytest.mark.asyncio
async def test_manifest_fingerprint_mismatch_falls_back_to_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    stale = [{"path": str(bundle), "size": 1, "sha256": "0000deadbeef"}]
    manifest = _write_manifest(tmp_path, _tools(), stale)
    monkeypatch.setenv("OFFICE_CLAW_MCP_MANIFEST_PATH", str(manifest))
    discovered = [{"name": "office_claw_post_message", "description": "live", "input_params": {}}]
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(return_value=discovered),
    )
    monkeypatch.setenv("JIUWENSWARM_MCP_SCHEMA_CACHE", "0")

    result = await list_office_claw_mcp_tools(_params(bundle))

    assert result == discovered


@pytest.mark.asyncio
async def test_manifest_missing_env_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    monkeypatch.delenv("OFFICE_CLAW_MCP_MANIFEST_PATH", raising=False)
    discovered = [{"name": "office_claw_post_message", "description": "live", "input_params": {}}]
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(return_value=discovered),
    )
    monkeypatch.setenv("JIUWENSWARM_MCP_SCHEMA_CACHE", "0")

    result = await list_office_claw_mcp_tools(_params(bundle))

    assert result == discovered


@pytest.mark.asyncio
async def test_manifest_applies_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    manifest = _write_manifest(tmp_path, _tools(), _bundle_fingerprint(bundle))
    monkeypatch.setenv("OFFICE_CLAW_MCP_MANIFEST_PATH", str(manifest))
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(side_effect=AssertionError("spawn should be skipped")),
    )

    result = await list_office_claw_mcp_tools(
        _params(bundle, excluded="office_claw_list_tasks")
    )

    names = [t["name"] for t in result]
    assert "office_claw_list_tasks" not in names
    assert names == ["office_claw_post_message", "limb_tool_guide"]


def test_load_manifest_returns_none_when_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    monkeypatch.setenv("OFFICE_CLAW_MCP_MANIFEST_PATH", str(tmp_path / "nope.json"))

    assert _load_office_claw_mcp_manifest(_params(bundle)) is None


@pytest.mark.asyncio
async def test_manifest_switch_disabled_falls_back_to_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    manifest = _write_manifest(tmp_path, _tools(), _bundle_fingerprint(bundle))
    monkeypatch.setenv("OFFICE_CLAW_MCP_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv("JIUWENSWARM_MCP_MANIFEST", "0")
    discovered = [{"name": "office_claw_post_message", "description": "live", "input_params": {}}]
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(return_value=discovered),
    )
    monkeypatch.setenv("JIUWENSWARM_MCP_SCHEMA_CACHE", "0")

    result = await list_office_claw_mcp_tools(_params(bundle))

    assert result == discovered


@pytest.mark.asyncio
async def test_manifest_switch_off_values_all_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    manifest = _write_manifest(tmp_path, _tools(), _bundle_fingerprint(bundle))
    monkeypatch.setenv("OFFICE_CLAW_MCP_MANIFEST_PATH", str(manifest))

    for off_val in ("0", "false", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv("JIUWENSWARM_MCP_MANIFEST", off_val)
        assert _load_office_claw_mcp_manifest(_params(bundle)) is None, f"should be disabled for {off_val!r}"


@pytest.mark.asyncio
async def test_manifest_switch_truthy_values_keep_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _write_bundle(tmp_path)
    manifest = _write_manifest(tmp_path, _tools(), _bundle_fingerprint(bundle))
    monkeypatch.setenv("OFFICE_CLAW_MCP_MANIFEST_PATH", str(manifest))
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(side_effect=AssertionError("spawn should be skipped")),
    )

    for on_val in ("1", "true", "yes", "on", ""):
        monkeypatch.setenv("JIUWENSWARM_MCP_MANIFEST", on_val)
        result = await list_office_claw_mcp_tools(_params(bundle))
        assert result is not None and len(result) == 3, f"should be enabled for {on_val!r}"
