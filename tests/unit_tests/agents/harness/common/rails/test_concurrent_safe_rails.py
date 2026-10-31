# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ConcurrentSafeSysOperationRail 的 read_file 像素注入默认值对齐测试。

任务 7：把「引用行 → 发送侧出像素」与 ReadFileTool 的缺省像素注入解耦，
默认不再拼 base64。此处只验证缺省分支（既未显式设 read_image_multimodal，
deep_config 也未设置该属性）时 ReadFileTool.enable_image_multimodal 为 False。
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


def test_read_tool_default_disables_image_multimodal() -> None:
    """缺省分支下 ReadFileTool 不再注入像素（enable_image_multimodal=False）。"""
    agent = _make_agent()
    rail = ConcurrentSafeSysOperationRail()  # _enable_read_image_multimodal 默认为 None
    rail.sys_operation = SimpleNamespace()  # 工具构造只赋值 operation，不做 fs 调用
    rail.workspace = None  # 走空 workspace_path 分支

    with _patch_resource_mgr(), \
            patch.object(csr, "WriteFileTool", _fake_tool_factory("write_file")), \
            patch.object(csr, "EditFileTool", _fake_tool_factory("edit_file")):
        rail.init(agent)

    read_tool = rail.tools[0]
    assert isinstance(read_tool, ReadFileTool)
    assert read_tool.enable_image_multimodal is False