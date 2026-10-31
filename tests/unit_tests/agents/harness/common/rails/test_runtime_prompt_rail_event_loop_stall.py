# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

"""验证 RuntimePromptRail.before_model_call 在慢盘场景下不阻塞事件循环（OA.05000010）。

背景（2026-10-15 生产事故）：AgentServer 每条消息的模型调用都经
``before_model_call`` 兜底读取配置模型名（``_configured_model_names`` →
``get_model_names`` → ``get_merged_config_dict``）。合并配置缓存 miss 时
（启动后首调 / ``clear_config_cache`` 失效后）双 YAML 读 + 合并（实测
~150ms）直接跑在事件循环上，慢盘场景放大为秒级停摆，请求超时且 pong 停发。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails import runtime_prompt_rail as _runtime_prompt_mod
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail
from jiuwenswarm.common import config as _config_mod

_SIMULATED_SLOW_READ_SECONDS = 1.5
_HEARTBEAT_INTERVAL = 0.05


class _FakeSession:
    def get_session_id(self) -> str:
        return "stall-session"


class _FakePromptAgent:
    def __init__(self, builder: SystemPromptBuilder) -> None:
        self.system_prompt_builder = builder
        self.prompt_attachment_manager = None


def _capture_runtime_setting(rail: RuntimePromptRail) -> list[str]:
    """抓下 runtime.setting 的内容（不依赖 attachment manager 是否可用）。"""
    captured: list[str] = []
    original = rail._upsert_prompt_attachment

    async def _recording(ctx, *, section, content, kind, priority):
        if section == "runtime.setting":
            captured.append(content)
        await original(ctx, section=section, content=content, kind=kind, priority=priority)

    rail._upsert_prompt_attachment = _recording  # type: ignore[method-assign]
    return captured


@pytest.fixture()
def isolated_merged_config_cache():
    """强制合并配置缓存 miss，并在测试后清掉本测试写入的缓存项。"""
    _config_mod.invalidate_merged_config_cache()
    yield
    _config_mod.invalidate_merged_config_cache()


@pytest.mark.asyncio
async def test_before_model_call_keeps_event_loop_responsive_on_slow_config_load(
    tmp_path, monkeypatch, isolated_merged_config_cache
):
    # state 文件不存在：runtime_state 读取走 FileNotFoundError（快），且
    # available_models 为空 → 触发 _configured_model_names 配置兜底路径。
    state_file = tmp_path / "missing-runtime-state.yaml"
    monkeypatch.setattr(
        _runtime_prompt_mod, "get_runtime_state_path", lambda session_id=None: state_file
    )

    # 模拟慢盘：合并配置缓存 miss 时，用户 override YAML 的读取耗时 1.5s
    # （模板那次读快，控制变量为单次慢读）。合并由模板驱动，模板必须也带
    # models 键，否则 override 的 models 会被丢掉。
    load_calls: list[object] = []

    def slow_load_yaml_dict(path, *args, **kwargs):
        load_calls.append(path)
        if len(load_calls) > 1:
            time.sleep(_SIMULATED_SLOW_READ_SECONDS)
        return {"models": {"stall-model": {}}}
    monkeypatch.setattr(_config_mod, "load_yaml_dict", slow_load_yaml_dict)

    builder = SystemPromptBuilder(language="cn")
    rail = RuntimePromptRail(language="cn", channel="web")
    rail.set_session_id("stall-session")
    rail.init(_FakePromptAgent(builder))
    captured = _capture_runtime_setting(rail)

    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.3)

    ctx = AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=None,
        session=_FakeSession(),
        extra={},
    )
    await rail.before_model_call(ctx)

    await asyncio.sleep(0.3)
    hb.cancel()
    try:
        await hb
    except asyncio.CancelledError:
        pass

    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "heartbeat never ticked"
    max_gap = max(gaps)
    assert max_gap < _SIMULATED_SLOW_READ_SECONDS, (
        f"event loop stalled {max_gap:.2f}s during config fallback read; "
        "config disk reads must not run on the event loop thread"
    )

    # 功能不回归：config 兜底模型名仍注入 runtime.setting
    assert captured, "runtime.setting 没有被注入"
    assert "stall-model" in captured[-1]
