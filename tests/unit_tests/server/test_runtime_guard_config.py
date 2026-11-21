# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DLL 沙箱开关判定与运行时审批应答路由。"""

import sys
from typing import ClassVar

import pytest
from openjiuwen.core.foundation.tool.execution_context import (
    bind_tool_execution_context,
    invoke_with_context,
)
from openjiuwen.core.sys_operation.local.process_launcher import create_process

from jiuwenswarm.common.config import is_dll_sandbox_enabled

# agent_shield 的 dll_guard 顶层依赖 ctypes.wintypes，非 Windows 无法导入。
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="DLL Guard is Windows-only")


def _config(startup_mode, enabled=True):
    return {"sandbox": {"startup_mode": startup_mode, "enabled": enabled}}


@pytest.fixture
def windows_personal(monkeypatch):
    """把开关判定固定到个人版 Windows，避免用例结果随 CI 平台漂移。"""
    monkeypatch.setattr("jiuwenswarm.common.config.is_enterprise", lambda: False)
    monkeypatch.setattr("jiuwenswarm.common.config.sys.platform", "win32")


def test_dll_mode_requires_both_switch_and_mode(windows_personal):
    assert is_dll_sandbox_enabled(_config("dll", True)) is True
    assert is_dll_sandbox_enabled(_config("dll", False)) is False
    assert is_dll_sandbox_enabled(_config("internal", True)) is False
    assert is_dll_sandbox_enabled(_config("external", True)) is False
    # 缺省与非法值都归一到 internal，不应误开 DLL 模式。
    assert is_dll_sandbox_enabled({"sandbox": {"enabled": True}}) is False
    assert is_dll_sandbox_enabled({"sandbox": {"startup_mode": "unknown", "enabled": True}}) is False


def test_non_boolean_switch_is_rejected(windows_personal):
    with pytest.raises(TypeError, match="sandbox.enabled"):
        is_dll_sandbox_enabled({"sandbox": {"startup_mode": "dll", "enabled": "true"}})


def test_dll_disabled_off_windows(monkeypatch):
    """个人版非 Windows 即使配置 dll 也不启用，避免跳过 jiuwenbox sysop。"""
    monkeypatch.setattr("jiuwenswarm.common.config.is_enterprise", lambda: False)
    monkeypatch.setattr("jiuwenswarm.common.config.sys.platform", "linux")
    assert is_dll_sandbox_enabled(_config("dll", True)) is False


def test_dll_is_personal_only(monkeypatch):
    """企业链路（Linux sidecar）即使配置残留 dll 也不能启用本机 Guard。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.is_enterprise", lambda: True
    )
    assert is_dll_sandbox_enabled({"sandbox": {"startup_mode": "dll", "enabled": True}}) is False


def test_jiuwenbox_mode_never_sees_dll(monkeypatch):
    """jiuwenbox sysop 只接受 internal/external，个人 dll 不应污染企业 sidecar。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_startup_mode", lambda: "dll"
    )
    from jiuwenswarm.common.config import get_jiuwenbox_startup_mode

    assert get_jiuwenbox_startup_mode() == "internal"


@WINDOWS_ONLY
def test_ordinary_request_is_not_a_runtime_reply():
    from jiuwenswarm.server.runtime.runtime_guard import resolve_runtime_reply

    assert resolve_runtime_reply("s", {"request_id": "chat-1", "answers": []}) is None
    assert resolve_runtime_reply("s", {}) is None


@WINDOWS_ONLY
def test_invalid_runtime_reply_is_consumed_as_rejected():
    from jiuwenswarm.server.runtime.runtime_guard import resolve_runtime_reply

    params = {"request_id": "dll-runtime:abc", "source": "permission_interrupt", "answers": "bad"}
    assert resolve_runtime_reply("s", params) is False
    # 合法形状但没有待处理请求：陈旧应答同样返回 False，不进入 Agent 分发。
    params = {
        "request_id": "dll-runtime:abc",
        "source": "permission_interrupt",
        "answers": [{"selected_options": ["本次允许"]}],
    }
    assert resolve_runtime_reply("s", params) is False


class _FakeJob:
    @staticmethod
    def active_processes():
        return False


class _FakeContext:
    execution_id = "exec-1"


class _FakeExecution:
    """记录 launch 调用，避免测试真的注入原生 DLL。"""

    instances: ClassVar[list] = []

    def __init__(self, **_kwargs):
        self.job = _FakeJob()
        self.context = _FakeContext()
        self.source_shell = None
        self.launched = False
        _FakeExecution.instances.append(self)

    async def __aenter__(self):
        return self

    async def close(self):
        return None

    async def launch(self, factory, *args, **kwargs):
        self.launched = True
        return await factory(*args, **kwargs)


class _FakeTool:
    """模拟 ability_manager 调用的工具：内部走 core 的 create_process。"""

    def __init__(self, result):
        self._result = result

    async def invoke(self, _inputs, *, session, tool_call_id):
        assert session is None and tool_call_id == "t1"
        return await create_process(self._factory)

    async def _factory(self):
        return self._result


@WINDOWS_ONLY
@pytest.mark.asyncio
async def test_guard_routes_host_process_launcher():
    """完整链路：invoke_with_context -> tool_execution -> core create_process。"""
    from agent_shield.intercepts.native_call.dll_guard import RuntimeGuardManager

    from jiuwenswarm.server.runtime import runtime_guard

    _FakeExecution.instances.clear()
    manager = RuntimeGuardManager(
        session_id="s",
        engine=lambda: None,
        dll="dummy.dll",
        enabled=lambda: True,
        execution_factory=_FakeExecution,
        bind_launcher=runtime_guard.bind_process_launchers,
    )
    marker = object()
    tool = _FakeTool(marker)
    with bind_tool_execution_context(manager.tool_execution):
        result = await invoke_with_context(
            tool, {}, session=None, tool_call_id="t1", tool_name="bash"
        )

    assert result is marker
    assert len(_FakeExecution.instances) == 1
    assert _FakeExecution.instances[0].launched is True


@WINDOWS_ONLY
def test_runtime_reply_normalizes_host_dataclass(monkeypatch):
    """宿主 frozen dataclass 必须先转成 agent-shield 模型，否则缺 wants_* 方法。"""
    from agent_shield.tool_permission.runtime.models import (
        PermissionConfirmResponse as ShieldConfirmResponse,
    )

    from jiuwenswarm.server.runtime import runtime_guard

    captured = {}

    def _resolve(session_id, request_id, answer):
        captured["answer"] = answer
        return True

    monkeypatch.setattr(runtime_guard.APPROVALS, "resolve", _resolve)
    params = {
        "request_id": "dll-runtime:abc",
        "source": "permission_interrupt",
        "answers": [{"selected_options": ["本次允许"]}],
    }
    assert runtime_guard.resolve_runtime_reply("s", params) is True
    assert isinstance(captured["answer"], ShieldConfirmResponse)
    assert hasattr(captured["answer"], "wants_permanent_persist")
    assert captured["answer"].authorization_scope is None


@WINDOWS_ONLY
def test_assemble_reuses_session_manager(monkeypatch):
    """同一会话重复装配必须复用 manager，不能重复创建注入会话。"""
    from types import SimpleNamespace

    from agent_shield.intercepts.native_call.dll_guard import RuntimeGuardManager

    from jiuwenswarm.server.runtime import runtime_guard

    manager = RuntimeGuardManager(
        session_id="s",
        engine=lambda: None,
        dll="dummy.dll",
        enabled=lambda: True,
        execution_factory=_FakeExecution,
    )
    monkeypatch.setattr(runtime_guard, "is_dll_sandbox_enabled", lambda: True)
    monkeypatch.setattr(runtime_guard, "require_dll_sandbox_runtime", lambda: "dummy")
    calls = []

    def _manager_for(*_args, **_kwargs):
        calls.append(1)
        return manager

    monkeypatch.setattr(runtime_guard, "_manager_for", _manager_for)
    instance = SimpleNamespace(invoke=lambda: None)

    for _ in range(2):
        assert (
            runtime_guard.assemble_guarded_agent(
                instance, session_id="s", permission_rail=None, workspace=None
            )
            is manager
        )
    assert len(calls) == 1
