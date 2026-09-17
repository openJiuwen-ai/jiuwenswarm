# -*- coding: utf-8 -*-
"""_export_tool_info 内容去重 + 原子写 + 降级行为。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment


def _make_env_with_tools(export_path: Path, tools: dict[str, str] | None = None) -> SkillTurboEnvironment:
    """构造最小 Environment：注册假工具卡，配置导出路径。"""
    env = SkillTurboEnvironment(
        {"tools": {}, "model": None, "tool_info_export_path": str(export_path)}
    )
    for name, desc in (tools or {"bash": "run bash"}).items():
        env.register_tool(SimpleNamespace(name=name, description=desc, input_params=None))
    return env


@pytest.fixture(autouse=True)
def _reset_export_cache():
    SkillTurboEnvironment._tool_info_last_export.clear()
    yield
    SkillTurboEnvironment._tool_info_last_export.clear()


def test_export_writes_valid_json(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "tool_info.json"
    env = _make_env_with_tools(target)
    env._export_tool_info()
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == [
        {"name": "bash", "description": "run bash"},
    ]
    # 无 tmp 残留
    assert list(target.parent.glob("*.tmp-*")) == []


def test_export_dedup_skips_rewrite(tmp_path: Path) -> None:
    target = tmp_path / "tool_info.json"
    env = _make_env_with_tools(target)
    env._export_tool_info()
    first_mtime = target.stat().st_mtime_ns

    # 新实例、相同内容：内容未变应跳过写盘（mtime 不变）
    env2 = _make_env_with_tools(target)
    env2._export_tool_info()
    assert target.stat().st_mtime_ns == first_mtime


def test_export_rewrites_on_schema_change(tmp_path: Path) -> None:
    target = tmp_path / "tool_info.json"
    env = _make_env_with_tools(target)
    env._export_tool_info()

    env2 = _make_env_with_tools(target, tools={"bash": "run bash", "grep": "search"})
    env2._export_tool_info()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [item["name"] for item in data] == ["bash", "grep"]


def test_export_rewrites_when_file_deleted_externally(tmp_path: Path) -> None:
    target = tmp_path / "tool_info.json"
    env = _make_env_with_tools(target)
    env._export_tool_info()
    target.unlink()  # 外部删除（如用户手动清理 workspace）

    env2 = _make_env_with_tools(target)
    env2._export_tool_info()
    assert target.is_file()


def test_export_failure_degrades_to_warning_not_raise(tmp_path: Path) -> None:
    env = _make_env_with_tools(tmp_path / "tool_info.json")
    # 模拟写盘失败（如 Windows 文件被占用）
    with patch.object(Path, "write_text", side_effect=PermissionError("locked")):
        env._export_tool_info()  # 不应抛出
    # 失败后不应污染缓存：下次正常调用仍可写出
    env._export_tool_info()
    assert (tmp_path / "tool_info.json").is_file()
