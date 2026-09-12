# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""spawn_fallback 权限挂起保全与 interrupt 结果识别的回归测试。

- ``_suspend_subagent_permission_rails`` 挂起时只应翻转 enabled，不得清空
  tools/file_guard（agent-core update_config 是整体替换语义）。
- interrupt result（``result_type == "interrupt"``）无 output 字段，
  ``spawn_fallback`` 应显式报错而非吞成空输出。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _FakeHost:
    def __init__(self) -> None:
        self.get_permissions_snapshot = lambda: {"enabled": True}


class PermissionInterruptRail:  # noqa: N801
    """最小权限 rail 替身：复现整体替换语义的 update_config。

    类名必须与生产匹配逻辑（cls.__name__ 白名单）一致，否则会被跳过。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._static_config = dict(config)
        self.engine_config = dict(config)
        self._host = _FakeHost()

    def update_config(self, config: dict[str, Any]) -> None:
        cfg = dict(config)
        self._static_config = cfg
        self.engine_config = cfg


class _FakeSubagent:
    def __init__(self, rails: list[Any]) -> None:
        self._rails = rails

    def configured_rails(self) -> list[Any]:
        return list(self._rails)


def test_suspend_preserves_full_config_only_flips_enabled() -> None:
    """挂起只翻转 enabled，tools/file_guard 必须保留在引擎配置中。"""
    rail = PermissionInterruptRail(
        {
            "enabled": True,
            "tools": {"web_search": "allow", "skill_acceleration_exec": "ask"},
            "file_guard": {"workspace": {"read": "allow"}},
        }
    )
    subagent = _FakeSubagent([rail])

    restore = JiuWenSwarmDeepAdapter._suspend_subagent_permission_rails(subagent)

    assert rail.engine_config["enabled"] is False
    assert rail.engine_config["tools"] == {
        "web_search": "allow",
        "skill_acceleration_exec": "ask",
    }
    assert rail.engine_config["file_guard"] == {"workspace": {"read": "allow"}}


def test_suspend_snapshot_callback_carries_full_config() -> None:
    """临时 snapshot 回调必须携带完整配置：resolve_interrupt 首次工具调用会
    用 host.get_permissions_snapshot 刷新引擎，返回残缺 dict 会二次清空。"""
    rail = PermissionInterruptRail(
        {
            "enabled": True,
            "tools": {"web_search": "allow"},
            "file_guard": {"workspace": {"read": "allow"}},
        }
    )
    subagent = _FakeSubagent([rail])

    restore = JiuWenSwarmDeepAdapter._suspend_subagent_permission_rails(subagent)

    snapshot = rail._host.get_permissions_snapshot()
    assert isinstance(snapshot, dict)
    assert snapshot["enabled"] is False
    assert snapshot["tools"] == {"web_search": "allow"}
    assert snapshot["file_guard"] == {"workspace": {"read": "allow"}}


def test_suspend_restore_returns_original_config_and_snapshot() -> None:
    """恢复后：引擎配置与 host snapshot 回到挂起前的原值。"""
    original = {
        "enabled": True,
        "tools": {"web_search": "allow"},
    }
    rail = PermissionInterruptRail(original)
    subagent = _FakeSubagent([rail])

    restore = JiuWenSwarmDeepAdapter._suspend_subagent_permission_rails(subagent)
    restore()

    assert rail.engine_config == original
    assert rail._host.get_permissions_snapshot() == {"enabled": True}


def test_suspend_ignores_unrelated_rails() -> None:
    """非权限类 rail 不参与挂起（保持既有行为）。"""

    class _OtherRail:
        pass

    other = _OtherRail()
    subagent = _FakeSubagent([other])

    restore = JiuWenSwarmDeepAdapter._suspend_subagent_permission_rails(subagent)
    restore()  # 无权限 rail 时恢复为 no-op，不应报错


class _FakeInstance:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    def create_subagent(self, kind: str, session_id: str) -> Any:
        self.created.append((kind, session_id))
        return _FakeSubagent([])


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    """spawn_fallback 仅依赖 self._instance；绕过重型 __init__。"""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = _FakeInstance()
    return adapter


@pytest.mark.asyncio
async def test_spawn_fallback_raises_on_interrupt_result(monkeypatch) -> None:
    """子代理以未解决的 HITL interrupt 结束时必须显式报错，不得吞成空输出。"""

    async def _fake_invoke(
        subagent: Any, *, inputs: dict, session: Any, source_label: str
    ) -> dict:
        # 复现 agent-core build_interrupt_result 的形状：无 output 字段
        return {"result_type": "interrupt", "state": [], "interrupt_ids": ["perm-1"]}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.debug_trace.invoke_subagent_with_trace",
        _fake_invoke,
    )
    adapter = _make_adapter()

    with pytest.raises(RuntimeError, match="HITL interrupt"):
        await adapter.spawn_fallback("请生成 PPT 内容策划")


@pytest.mark.asyncio
async def test_spawn_fallback_returns_output_normally(monkeypatch) -> None:
    """正常结果（含 output）不受影响：返回输出文本（不得回归）。"""

    async def _fake_invoke(
        subagent: Any, *, inputs: dict, session: Any, source_label: str
    ) -> dict:
        return {"output": "节点执行完成", "result_type": "done"}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.debug_trace.invoke_subagent_with_trace",
        _fake_invoke,
    )
    adapter = _make_adapter()

    output = await adapter.spawn_fallback("请生成 PPT 内容策划")

    assert output == "节点执行完成"
