# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ConcurrentSafeSysOperationRail 的 read_file 像素注入动态解析测试。

任务 7：把「引用行 → 发送侧出像素」与 ReadFileTool 的缺省像素注入解耦，
默认不再拼 base64。此处验证两点：
1. 缺省分支（未显式设 read_image_multimodal、deep_config 也未设）为 False；
2. resolver 跟随 deep_config 运行时变化——模型热切换后 read_file 行为跟随，
   不再冻结在 rail init 时的值。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from openjiuwen.harness.tools.filesystem import ReadFileTool

from jiuwenswarm.agents.harness.common.rails import concurrent_safe_rails as csr
from jiuwenswarm.agents.harness.common.rails.concurrent_safe_rails import (
    ConcurrentSafeSysOperationRail,
)


def _make_agent() -> SimpleNamespace:
    """构造不带 enable_read_image_multimodal 的最小 agent 替身。"""
    ability_manager = SimpleNamespace(added=[])
    ability_manager.add = lambda card: ability_manager.added.append(card)
    return SimpleNamespace(
        system_prompt_builder=SimpleNamespace(language="cn"),
        card=SimpleNamespace(id=f"test-agent-{uuid.uuid4().hex}"),
        deep_config=SimpleNamespace(),  # 无 enable_read_image_multimodal 属性
        ability_manager=ability_manager,
    )


def _fake_tool_factory(name: str):
    """隔离与本次被测逻辑无关的工具构造（含 workspace_path 签名差异）。"""

    def factory(operation, language="cn", agent_id=None, workspace_path=None):
        return SimpleNamespace(
            operation=operation,
            card=SimpleNamespace(id=f"{name}-{uuid.uuid4().hex}"),
        )

    return factory


def _patch_resource_mgr():
    """隔离全局 resource_mgr 副作用；不涉及被测的缺省值逻辑。"""
    return patch(
        "openjiuwen.core.runner.Runner.resource_mgr",
        SimpleNamespace(
            get_tool=lambda *args, **kwargs: None,
            add_tool=lambda *args, **kwargs: None,
        ),
    )


def _build_rail(agent) -> ConcurrentSafeSysOperationRail:
    rail = ConcurrentSafeSysOperationRail()  # _enable_read_image_multimodal 默认为 None
    rail.sys_operation = SimpleNamespace()  # 工具构造只赋值 operation，不做 fs 调用
    rail.workspace = None  # 走空 workspace_path 分支
    with _patch_resource_mgr(), \
            patch.object(csr, "WriteFileTool", _fake_tool_factory("write_file")), \
            patch.object(csr, "EditFileTool", _fake_tool_factory("edit_file")):
        rail.init(agent)
    return rail


def test_read_tool_default_disables_image_multimodal() -> None:
    """缺省分支下 ReadFileTool 不再注入像素（enable_image_multimodal=False）。"""
    agent = _make_agent()
    read_tool = _build_rail(agent).tools[0]

    assert isinstance(read_tool, ReadFileTool)
    assert read_tool.enable_image_multimodal is False
    assert read_tool._image_multimodal_enabled() is False


def test_read_tool_resolver_follows_model_switch() -> None:
    """deep_config 切换后，resolver 让 read_file 立刻跟随，无需重建 rail。"""
    agent = _make_agent()
    read_tool = _build_rail(agent).tools[0]

    agent.deep_config.enable_read_image_multimodal = True
    assert read_tool._image_multimodal_enabled() is True

    agent.deep_config.enable_read_image_multimodal = False
    assert read_tool._image_multimodal_enabled() is False


def test_explicit_rail_flag_wins_over_deep_config() -> None:
    """显式传入 rail 构造参数时保持既有语义：显式值优先于 deep_config。"""
    agent = _make_agent()
    agent.deep_config.enable_read_image_multimodal = False
    rail = ConcurrentSafeSysOperationRail(enable_read_image_multimodal=True)
    rail.sys_operation = SimpleNamespace()
    rail.workspace = None
    with _patch_resource_mgr(), \
            patch.object(csr, "WriteFileTool", _fake_tool_factory("write_file")), \
            patch.object(csr, "EditFileTool", _fake_tool_factory("edit_file")):
        rail.init(agent)

    assert rail.tools[0]._image_multimodal_enabled() is True
