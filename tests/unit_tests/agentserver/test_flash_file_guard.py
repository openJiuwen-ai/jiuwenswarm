# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""All paths are checked before any batch read begins."""

from unittest.mock import AsyncMock

import pytest
import test_flash_permission_actions as actions
from flash_sdk import sdk_tools

from jiuwenswarm.agents.harness.flash.tools.flash_read_tool import FlashReadFileTool

permission_adapter = actions.permission_adapter
tool_context = actions.tool_context


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("batch", [False, True])
async def test_mixed_batch_no_read_before_all_resources_approved(
    permission_adapter, tmp_path, monkeypatch, legacy, symlink, batch
):
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    a, b = allowed / "a.txt", denied / "b.txt"
    a.write_text("fixture-a")
    b.write_text("fixture-b")
    target = b
    if symlink:
        target = allowed / "link.txt"
        target.symlink_to(b)
    # mode 契约（评审 CR-015 修复）下 preset 主导缺省轴，但显式 deny
    # 取严合并存活：legacy 分支经 external_directory '*'→defaults 映射，
    # native 分支走 paths 逐路径 deny（longest-prefix + deny 优先）。
    config = (
        {"external_directory": {"*": "deny"}}
        if legacy
        else {
            "file_guard": {
                "enabled": True,
                "paths": [
                    {"path": str(allowed), "read": "allow"},
                    {"path": str(tmp_path), "read": "deny"},
                ],
            }
        }
    )
    _, rail = permission_adapter({"read_file": "allow"}, **config)
    rail._engine._workspace_root = allowed
    rail.update_config(rail._static_config)
    tool = FlashReadFileTool(operation=object(), agent_id="flash-test")
    read = AsyncMock(
        return_value={"file_path": str(a), "content": "fixture-a", "error": None}
    )
    monkeypatch.setattr(tool, "_read_one_file", read)
    ctx = tool_context(
        "read_file",
        {"file_paths": [str(a), str(target)]} if batch else {"file_path": str(target)},
    )
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        result = await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        read.assert_not_awaited()
        assert "PERMISSION_DENIED" in str(result)


@pytest.mark.asyncio
async def test_allowed_batch_and_single_have_equivalent_decisions(
    permission_adapter, tmp_path
):
    from openjiuwen.harness.security.models import PermissionLevel

    _, rail = permission_adapter(
        {"read_file": "allow"},
        file_guard={
            "enabled": True,
            "defaults": {"read": "deny"},
            "paths": [{"path": str(tmp_path), "read": "allow"}],
        },
    )
    paths = [str(tmp_path / name) for name in ("a", "b")]
    for args in ({"file_path": paths[0]}, {"file_paths": paths}):
        assert (
            await rail._engine.check_permission("read_file", args)
        ).permission == PermissionLevel.ALLOW


@pytest.mark.asyncio
async def test_approved_batch_reads_each_fixture_once(
    permission_adapter, tmp_path, monkeypatch
):
    from pathlib import Path

    from openjiuwen.harness.security.models import PermissionConfirmResponse

    paths = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for index, path in enumerate(paths):
        path.write_text(f"fixture-{index}")
    _, rail = permission_adapter(
        {"read_file": "allow"},
        file_guard={
            "enabled": True,
            "defaults": {"read": "ask"},
        },
    )
    reads = []

    async def read(path, model):
        reads.append(path)
        return {"file_path": path, "content": Path(path).read_text(), "error": None}

    async def confirm(request):
        assert reads == []
        assert request.result.external_paths and len(request.result.external_paths) == 2
        return PermissionConfirmResponse(approved=True)

    rail._host.request_permission_confirmation = confirm
    tool = FlashReadFileTool(operation=object(), agent_id="flash-test")
    monkeypatch.setattr(tool, "_read_one_file", read)
    ctx = tool_context("read_file", {"file_paths": [str(path) for path in paths]})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        result = await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        assert sorted(reads) == sorted(str(path) for path in paths), str(result)
