"""RequestIdentityRail 单元测试（请求身份重绑，无条件常挂）。

背景：模型调用发生在 supervisor 派生的 round task 上，``ContextVar.set`` 只改
当前 task 副本，handler 上逐请求的绑定传不进早已创建的长生命周期 task。本
rail 在 ``before_invoke``（round task、模型调用前）把
``run_context.extra["request_id"]`` 抄进请求身份 ContextVar，使
``before_model_http`` 埋点与 llm_io_trace 在本 task 上读到正确身份。

验收要点：
- 注入 request_id 时重绑 openjiuwen 当前请求 id；未注入时不动现有绑定。
- ctx 畸形（无 inputs / 无 run_context / 无 extra）不抛错（fail-open）。
- openjiuwen set_current_request_id 抛错只降级 debug，不影响请求。
- llm_io_trace 的 ``_LLM_TRACE_REQUEST_ID`` 联动重绑：sys.modules 探测，
  模块未加载 / 未暴露该属性时静默跳过。
- priority 高于 ModelRoutingRail，重绑先于路由决策。
- deep adapter 的构建器无条件返回本 rail（不受 model_routing 开关约束）。
"""
from __future__ import annotations

import sys
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.request_identity_rail import (
    RequestIdentityRail,
    bind_llm_trace_request_id,
    bind_request_id_from_run_context,
)

_LLM_TRACE_MODULE = "jiuwenswarm.server.runtime.agent_adapter.interface_deep"


def _ctx(extra: dict[str, Any]) -> SimpleNamespace:
    """单 agent 链路的假 ctx：inputs.run_context.extra 携带逐请求注入值。"""
    return SimpleNamespace(
        inputs=SimpleNamespace(
            query="你好",
            run_context=SimpleNamespace(extra=extra),
            response=None,
        ),
        extra={},
    )


# ---- openjiuwen request_id 重绑 ---- #


@pytest.mark.asyncio
async def test_before_invoke_rebinds_openjiuwen_request_id():
    rail = RequestIdentityRail()
    from openjiuwen.extensions.observability.span_context import (
        clear_current_request_id,
        get_current_request_id,
    )

    try:
        await rail.before_invoke(_ctx({"request_id": "req-42"}))
        assert get_current_request_id() == "req-42"
        # 幂等：同值重复绑定无害
        await rail.before_invoke(_ctx({"request_id": "req-42"}))
        assert get_current_request_id() == "req-42"
    finally:
        clear_current_request_id()


@pytest.mark.asyncio
async def test_before_invoke_without_request_id_keeps_existing_binding():
    """extra 未注入 request_id → 不动现有绑定（不写空值覆盖陈旧上下文）。"""
    from openjiuwen.extensions.observability.span_context import (
        clear_current_request_id,
        get_current_request_id,
        set_current_request_id,
    )

    set_current_request_id("req-stale")
    rail = RequestIdentityRail()
    try:
        await rail.before_invoke(_ctx({"model_selection": "auto"}))
        await rail.before_invoke(_ctx({}))
        await rail.before_invoke(_ctx({"request_id": ""}))
        assert get_current_request_id() == "req-stale"
    finally:
        clear_current_request_id()


@pytest.mark.asyncio
async def test_before_invoke_tolerates_malformed_context():
    """ctx 畸形（无 inputs / 无 run_context / 无 extra）静默跳过，不抛错。"""
    rail = RequestIdentityRail()
    await rail.before_invoke(SimpleNamespace())
    await rail.before_invoke(SimpleNamespace(inputs=SimpleNamespace()))
    await rail.before_invoke(
        SimpleNamespace(inputs=SimpleNamespace(run_context=SimpleNamespace()))
    )


@pytest.mark.asyncio
async def test_bind_fail_open_when_openjiuwen_set_raises(monkeypatch):
    """openjiuwen 侧 set 抛错只降级 debug，绝不影响请求执行。"""
    import openjiuwen.extensions.observability.span_context as span_context

    def _boom(_request_id: str | None) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(span_context, "set_current_request_id", _boom)
    rail = RequestIdentityRail()
    await rail.before_invoke(_ctx({"request_id": "req-1"}))  # 不抛即通过


# ---- llm_io_trace 联动重绑（sys.modules 探测）---- #


@pytest.mark.asyncio
async def test_llm_trace_rebound_when_module_loaded(monkeypatch):
    """interface_deep 已加载且暴露 _LLM_TRACE_REQUEST_ID → 同值重绑。"""
    var: ContextVar[str] = ContextVar("test_llm_trace_rid", default="")
    fake = SimpleNamespace(_LLM_TRACE_REQUEST_ID=var)
    monkeypatch.setitem(sys.modules, _LLM_TRACE_MODULE, fake)

    rail = RequestIdentityRail()
    await rail.before_invoke(_ctx({"request_id": "req-7"}))
    assert var.get() == "req-7"


@pytest.mark.asyncio
async def test_llm_trace_skipped_when_module_absent(monkeypatch):
    """interface_deep 未加载（非 AgentServer 进程）→ 静默跳过。"""
    monkeypatch.delitem(sys.modules, _LLM_TRACE_MODULE, raising=False)
    rail = RequestIdentityRail()
    await rail.before_invoke(_ctx({"request_id": "req-8"}))  # 不抛即通过


@pytest.mark.asyncio
async def test_llm_trace_skipped_when_attribute_missing(monkeypatch):
    """模块加载了但没有该属性（上游改名）→ 静默跳过，不炸请求。"""
    monkeypatch.setitem(sys.modules, _LLM_TRACE_MODULE, SimpleNamespace())
    rail = RequestIdentityRail()
    await rail.before_invoke(_ctx({"request_id": "req-9"}))  # 不抛即通过


def test_bind_llm_trace_request_id_fail_open():
    """探测路径上的任何异常都被吞掉（fail-open），不影响请求。"""
    # 塞一个 .set 会抛错的假 ContextVar 进 sys.modules：绑定失败必须静默
    class _BadVar:
        def set(self, _value: str) -> None:
            raise RuntimeError("bad var")

    fake = SimpleNamespace(_LLM_TRACE_REQUEST_ID=_BadVar())
    original = sys.modules.get(_LLM_TRACE_MODULE)
    sys.modules[_LLM_TRACE_MODULE] = fake
    try:
        assert bind_llm_trace_request_id("req-10") is None  # 不抛即通过
    finally:
        if original is not None:
            sys.modules[_LLM_TRACE_MODULE] = original
        else:
            sys.modules.pop(_LLM_TRACE_MODULE, None)


# ---- 共享 helper 直测 ---- #


def test_bind_request_id_from_run_context_returns_rid():
    from openjiuwen.extensions.observability.span_context import (
        clear_current_request_id,
        get_current_request_id,
    )

    try:
        assert bind_request_id_from_run_context(_ctx({"request_id": "req-77"})) == "req-77"
        assert get_current_request_id() == "req-77"
    finally:
        clear_current_request_id()


def test_bind_request_id_from_run_context_returns_none_when_absent():
    assert bind_request_id_from_run_context(_ctx({})) is None
    assert bind_request_id_from_run_context(SimpleNamespace()) is None


# ---- 挂载契约 ---- #


def test_priority_runs_before_model_routing():
    """重绑必须先于路由决策（ModelRoutingRail.priority=95）完成。"""
    from jiuwenswarm.agents.harness.common.rails.model_routing.model_routing_rail import (
        ModelRoutingRail,
    )

    assert RequestIdentityRail.priority > ModelRoutingRail.priority


def test_deep_adapter_builds_request_identity_rail():
    """deep adapter 的构建器无条件返回 RequestIdentityRail（不受 model_routing 开关约束）。"""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    rail = JiuWenSwarmDeepAdapter._build_request_identity_rail()
    assert isinstance(rail, RequestIdentityRail)


def test_code_adapter_mounts_request_identity_rail():
    """code 模式固定 rail 表首位挂载身份重绑 rail（PR8595 审查 P2 补齐装配面）。

    code 模式复用继承的 process_message_impl / _process_message_stream_impl，
    ``run_context.extra["request_id"]`` 注入同样覆盖 code 请求；固定表不挂本
    rail，warm-pool worker 上的埋点/llm_io_trace 归因维持陈旧值（审查指出的
    装配缺口）。team 子模式共用固定表，一并覆盖。
    """
    import openjiuwen.agent_evolving.trajectory as _traj_mod

    if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):  # pragma: no cover
        from unittest.mock import MagicMock

        _traj_mod.InMemoryTrajectoryRegistry = MagicMock

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    captured: list = []
    real_instantiate = adapter._instantiate_rails

    def _capture(rail_infos, _config_base):
        captured.extend(rail_infos)
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _capture)
    try:
        adapter._build_agent_rails(config={}, config_base={}, mode="code")
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)

    matches = [i for i in captured if i.attr_name == "_request_identity_rail"]
    assert len(matches) == 1
    assert matches[0].build_func == adapter._build_request_identity_rail
    # 首位挂载，与 deep 基类表一致（priority=100 早于所有 rail 执行）
    assert captured[0].attr_name == "_request_identity_rail"
