# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""原生图片输入（[[S_22]]）读图工具能力接线。

回归焦点：``read_file`` 的内联像素判定跟随主模型切换的前提是 core 提供
``image_multimodal_resolver``（调用时求值）。锁定 core（``uv.lock`` 的 8215f2c）
只有静态 ``enable_image_multimodal: bool``，构造时求值后即冻结——这是该版本的固有
语义，不是本模块的缺陷；core 合入 resolver 并刷新 uv.lock 后自动获得热切换。

deep 走 ``FlashBashSysOperationRail``（替换 stock ReadFileTool）、flash 走
``SlimSysOperationRail``（自建 FlashReadFileTool），新 core 上两条路径都必须把
resolver 透传下去；锁定 core 上退回构造时求值。
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common import read_file_image_support as support
from jiuwenswarm.agents.harness.common.read_file_image_support import (
    build_read_file_image_kwargs,
    read_file_image_multimodal_enabled,
)
from jiuwenswarm.agents.harness.flash.rails.slim_sys_operation_rail import (
    SlimSysOperationRail,
)
from jiuwenswarm.agents.harness.flash.tools.flash_bash_tool import (
    FlashBashSysOperationRail,
)
from jiuwenswarm.agents.harness.flash.tools.flash_read_tool import FlashReadFileTool


class _FakeAbilityManager:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def add_ability(self, card, tool) -> None:
        self.tools[getattr(card, "name", str(id(tool)))] = tool

    def remove_ability(self, name: str) -> None:
        self.tools.pop(name, None)


def _fake_agent(enable: bool) -> SimpleNamespace:
    return SimpleNamespace(
        system_prompt_builder=SimpleNamespace(language="cn"),
        card=SimpleNamespace(id="agent-native-image-test"),
        ability_manager=_FakeAbilityManager(),
        deep_config=SimpleNamespace(enable_read_image_multimodal=enable),
    )


def _flash_read_tool(rail) -> FlashReadFileTool:
    for tool in rail.tools or []:
        if isinstance(tool, FlashReadFileTool):
            return tool
    raise AssertionError("rail 未注册 FlashReadFileTool")


# ---------------------------------------------------------------------------
# build_read_file_image_kwargs：跨 core 版本的 resolver 透传
# ---------------------------------------------------------------------------


def test_kwargs_without_resolver_is_static_bool() -> None:
    assert build_read_file_image_kwargs(enable_image_multimodal=False) == {
        "enable_image_multimodal": False,
    }
    assert build_read_file_image_kwargs(enable_image_multimodal=True) == {
        "enable_image_multimodal": True,
    }


def test_kwargs_passes_resolver_to_dedicated_param_when_core_supports(
    monkeypatch,
) -> None:
    """新 core：resolver 走专用 image_multimodal_resolver 键 + 静态兜底。"""

    monkeypatch.setattr(support, "_supports_resolver", True)

    def resolver() -> bool:
        return False

    assert build_read_file_image_kwargs(resolver, enable_image_multimodal=True) == {
        "enable_image_multimodal": True,
        support.IMAGE_MULTIMODAL_RESOLVER_PARAM: resolver,
    }


def test_kwargs_omits_resolver_when_core_locked(monkeypatch) -> None:
    """锁定 core（无 image_multimodal_resolver）：resolver 被丢弃，退回构造时求值。"""

    monkeypatch.setattr(support, "_supports_resolver", False)

    def resolver() -> bool:
        return False

    assert build_read_file_image_kwargs(resolver, enable_image_multimodal=False) == {
        "enable_image_multimodal": False,
    }


# ---------------------------------------------------------------------------
# rail 集成：resolver 透传后 read_file 跟随模型热切换
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not support.supports_read_file_image_resolver(),
    reason="锁定 core（8215f2c）无 image_multimodal_resolver，热切换能力未启用",
)
def test_flash_bash_rail_read_file_follows_model_switch() -> None:
    """deep profile：新 core 替换 stock ReadFileTool 时继承 resolver，切模型后行为跟随。"""

    agent = _fake_agent(enable=True)
    rail = FlashBashSysOperationRail()
    rail.sys_operation = SimpleNamespace()

    rail.init(agent)

    read_tool = _flash_read_tool(rail)
    assert read_file_image_multimodal_enabled(read_tool) is True

    agent.deep_config.enable_read_image_multimodal = False
    assert read_file_image_multimodal_enabled(read_tool) is False


@pytest.mark.skipif(
    not support.supports_read_file_image_resolver(),
    reason="锁定 core（8215f2c）无 image_multimodal_resolver，热切换能力未启用",
)
def test_slim_rail_read_file_follows_model_switch() -> None:
    """flash profile：新 core 下 init 不冻结能力值，切模型后 read_file 行为跟随。"""

    agent = _fake_agent(enable=False)
    rail = SlimSysOperationRail()
    rail.sys_operation = SimpleNamespace()

    rail.init(agent)

    read_tool = _flash_read_tool(rail)
    assert read_file_image_multimodal_enabled(read_tool) is False

    agent.deep_config.enable_read_image_multimodal = True
    assert read_file_image_multimodal_enabled(read_tool) is True


def test_slim_rail_explicit_flag_still_wins_over_model_capability() -> None:
    """显式 enable_read_image_multimodal=False 是合法降级，不被模型能力覆盖。"""

    agent = _fake_agent(enable=True)
    rail = SlimSysOperationRail(enable_read_image_multimodal=False)
    rail.sys_operation = SimpleNamespace()

    rail.init(agent)

    read_tool = _flash_read_tool(rail)
    assert read_file_image_multimodal_enabled(read_tool) is False
