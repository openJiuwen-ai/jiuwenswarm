# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""E2E-ish: 真实 seed zip + prepare_workspace 的 mcp_builtins 接入点.

跑在临时工作区, 不碰用户 ~/.jiuwenswarm. 验证:
1. prepare_workspace 调用链会把 seed zip 解到 mcp_builtins 目录
2. list_marketplace_mcps 能读到解出来的包(华为云/鸿蒙置顶排序)
3. 二次调用版本一致时跳过(不重复解压)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.common.utils import prepare_workspace


@pytest.fixture()
def temp_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 ~/.jiuwenswarm 等价目录; 隔离 get_user_workspace_dir."""
    ws = tmp_path / "jiuwenswarm_home"
    # Pre-create the config/ subdir so _resolve_paths() (called the first time
    # anyone hits get_workspace_dir() / get_config_dir()) takes the
    # "already-initialized user workspace" branch and pins _workspace_dir to
    # <tmp>/agent/workspace — NOT the package's bundled resources/agent/workspace.
    # Without this, a clean CI box (no ~/.jiuwenswarm yet) would fall through to
    # the resources branch, and list_marketplace_mcps / _packages_dir() would
    # read the package's bundled mcp_builtins (empty / not extracted) instead of
    # the tmp workspace prepare_workspace just populated — yielding 0 packages
    # locally-but-not-on-CI (local boxes have a ~/.jiuwenswarm/config that
    # accidentally satisfies the branch).
    (ws / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: ws
    )
    # reset cached path resolver so get_workspace_dir picks up the new root.
    import jiuwenswarm.common.utils as u
    monkeypatch.setattr(u, "_initialized", False)
    monkeypatch.setattr(u, "_config_dir", None)
    monkeypatch.setattr(u, "_workspace_dir", None)
    monkeypatch.setattr(u, "_root_dir", None)
    # non-interactive language prompt.
    monkeypatch.setattr(u, "_is_interactive", lambda: False)
    monkeypatch.setattr(u, "prompt_preferred_language", lambda: "zh")
    return ws


def test_prepare_workspace_extracts_mcp_builtins(temp_workspace: Path) -> None:
    """prepare_workspace 跑完后, mcp_builtins 应从 seed zip 解压就位."""
    prepare_workspace(overwrite=True, preferred_language="zh", workspace_dir=temp_workspace)

    mcp_builtins = temp_workspace / "agent" / "workspace" / "mcp" / "mcp_builtins"
    assert mcp_builtins.is_dir(), "mcp_builtins 未解压"
    assert (mcp_builtins / "index.json").is_file(), "index.json 缺失"
    # 至少有几个真实 MCP 包目录.
    pkg_dirs = [p for p in mcp_builtins.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert len(pkg_dirs) >= 10, f"包目录太少: {len(pkg_dirs)}"


def test_list_marketplace_orders_huawei_first(temp_workspace: Path) -> None:
    """解压后 mcp.list 应把 huaweiyun-mcp / harmonyos-mcp 排在最前."""
    prepare_workspace(overwrite=True, preferred_language="zh", workspace_dir=temp_workspace)
    # 重置 registry 缓存路径指向临时工作区.
    import jiuwenswarm.server.runtime.mcp.registry as reg
    from jiuwenswarm.common.utils import get_workspace_dir
    # registry 的 _packages_dir 依赖 get_workspace_dir, 已被 fixture 重定向.
    names = [m["name"] for m in reg.list_marketplace_mcps("builtin")]
    assert names, "空列表"
    assert names[0] in ("huaweiyun-mcp", "harmonyos-mcp"), f"置顶失效, 首: {names[0]}"
    assert names[1] in ("huaweiyun-mcp", "harmonyos-mcp"), f"第二非华为系: {names[1]}"
    # 确认两个华为系都在且相邻置顶.
    assert set(names[:2]) == {"huaweiyun-mcp", "harmonyos-mcp"}


def test_second_prepare_skips_when_version_matches(temp_workspace: Path) -> None:
    """版本一致时二次调用不应破坏用户在包目录里加的脏文件."""
    prepare_workspace(overwrite=True, preferred_language="zh", workspace_dir=temp_workspace)
    mcp_builtins = temp_workspace / "agent" / "workspace" / "mcp" / "mcp_builtins"
    dirty = mcp_builtins / "dirty-survives.txt"
    dirty.write_text("keep", encoding="utf-8")

    # 第二次: overwrite=False, 版本一致 -> 跳过解压, 脏文件保留.
    prepare_workspace(overwrite=False, preferred_language="zh", workspace_dir=temp_workspace)
    assert dirty.read_text(encoding="utf-8") == "keep"


def test_prepare_workspace_leaves_no_seed_zip_leftover(temp_workspace: Path) -> None:
    """种子 zip 必须留在 resources 包内原地址解压, 不拷到用户工作区根.

    拷贝 template workspace 时 ignore 掉 mcp_builtins*.zip —— 该 zip 是
    _ensure_mcp_builtins 的种子, 直接从 template 原地址读并解压到
    mcp/mcp_builtins/, 不应在用户工作区根留一份 4MB 无用途拋留。
    """
    prepare_workspace(overwrite=True, preferred_language="zh", workspace_dir=temp_workspace)

    ws_root = temp_workspace / "agent" / "workspace"
    leftovers = list(ws_root.glob("mcp_builtins*.zip"))
    assert not leftovers, f"seed zip leaked to workspace root: {leftovers}"
    # 解压目录仍在, 且内容完整。
    mcp_builtins = ws_root / "mcp" / "mcp_builtins"
    assert (mcp_builtins / "index.json").is_file()

