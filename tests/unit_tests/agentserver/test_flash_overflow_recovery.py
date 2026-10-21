# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash 模式 413 上下文溢出恢复 + LLM 重试兜底的故障注入测试（F05）。

背景：``_FLASH_RAIL_KEEP`` 原本裁掉 ``_context_overflow_recovery_rail`` /
``_llm_retry_rail``，flash 只剩 0.8 阈值的主动压缩——单条巨型工具结果一步跨过
阈值、或压缩调用自身失败时，413 直接抛给用户，且上下文保持超限 → 会话内每次
后续调用同样失败（无自愈）。本组测试验证补挂两个 rail 后的完整兜底链。

覆盖：
- 挂载：两个 rail 进白名单；父类 ``_build_agent_rails`` 全表经 flash 白名单过滤
  后存活，且与 ``_context_processor_rail`` 成对（恢复 rail 靠其中的
  FullCompactProcessor 兜底——该处理器仅溢出恢复路径触发，日常 add 不跑）。
- 工厂去重契约：NotifyingLLMRetryRail 必须是 SDK LLMRetryRail 子类，否则
  ``create_deep_agent(enable_llm_retry_rail=True)`` 的 ``_already_provided``
  （issubclass）去重失效，flash 会叠双份重试循环。
- 恢复链：413 三种错误格式（Anthropic / 华为 / ModelArts.81001）→
  set_force_compact + threshold_override=0.85×limit + request_retry；模型成功后
  计数清零；持续溢出按 max_recovery_attempts 熔断并向前端推
  context_overflow_circuit_break 错误帧；桥接缺失时有界失败不崩溃。
- 重试 rail：瞬时连接错误有界重试（预算按 ctx 隔离）；认证错误 / 用户取消
  不重试直接传播；重试过程对用户可见（retry_notification 帧）。
- builder 配置化：``execution_guard.context_overflow_recovery.max_attempts``
  生效，缺省 / 非法 / <=0 一律回落 3。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
)
from openjiuwen.harness.rails.llm_retry_rail import LLMRetryRail

from jiuwenswarm.agents.harness.common.rails.context_overflow_recovery_rail import (
    ContextOverflowRecoveryRail,
)
from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import (
    NotifyingLLMRetryRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)

# RECOVERY_THRESHOLD_RATIO：413 恢复时 threshold_override = 模型窗口 × 0.85
_RATIO = 0.85


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _OverflowError(Exception):
    """带可选 status_code 的伪模型异常。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class _FakeFullCompactProcessor:
    """桩 FullCompactProcessor：记录 force_compact / threshold_override 调用。"""

    def __init__(self):
        self.force_compact = None
        self.overflow_threshold_override = None

    @staticmethod
    def processor_type() -> str:
        return "FullCompactProcessor"

    def set_force_compact(self, value: bool) -> None:
        self.force_compact = value

    def set_overflow_threshold_override(self, value: int) -> None:
        self.overflow_threshold_override = value


class _FakeSession:
    """捕获 write_stream 下发帧（retry_notification / error）。"""

    def __init__(self):
        self.streams: list = []

    async def write_stream(self, schema) -> None:
        self.streams.append(schema)


def _ctx(
    exception: BaseException | None = None,
    context=None,
    session=None,
) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=None,
        inputs=ModelCallInputs(messages=[], tools=None, model_context=None),
        context=context,
        extra={},
        session=session,
        exception=exception,
    )


def _retry_rail(max_retries: int = 2) -> NotifyingLLMRetryRail:
    return NotifyingLLMRetryRail(
        max_retries=max_retries,
        backoff_seconds=[0.0] * max_retries,
        notify_user_on_retry=False,
        notify_user_on_exhausted=False,
    )


_ANTHROPIC_413 = "prompt is too long: 90000 tokens > 80000 maximum"
_HUAWEI_OVERFLOW = "prompt length 90000 must less than the maximum input length 80000"


# ---------------------------------------------------------------------------
# 挂载：白名单 + 父类 rail 全表过滤存活
# ---------------------------------------------------------------------------


def test_keep_whitelist_contains_recovery_and_retry_rails():
    """两个兜底 rail 必须进 flash 白名单（丢失 = 无 413 恢复 / 无重试）。"""
    keep = JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP
    assert "_context_overflow_recovery_rail" in keep
    assert "_llm_retry_rail" in keep


def test_flash_rail_list_survives_whitelist_filter():
    """父类 _build_agent_rails 全表经 flash 白名单过滤后，兜底 rail 存活。

    桩 _instantiate_rails 原样返回 rail_infos（不触发真实构建的重依赖），分别
    校验：父类全表确有这两个条目（白名单不是空挂）；过滤后仍保留；且桥接目标
    _context_processor_rail 同样保留——恢复 rail 靠它里面的 FullCompactProcessor
    做实际压缩，只留恢复 rail 不留 processor 则压缩无从发生。
    """
    adapter = JiuwenSwarmFlashAdapter()
    captured: list = []
    real_instantiate = adapter._instantiate_rails

    def _passthrough(rail_infos, config_base, *args, **kwargs):
        captured.extend(rail_infos)
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _passthrough)
    try:
        adapter._build_agent_rails(config={}, config_base={}, mode="flash")
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)

    names = [info.attr_name for info in captured]
    assert "_context_overflow_recovery_rail" in names
    assert "_llm_retry_rail" in names

    kept = {info.attr_name for info in adapter._filter_rail_infos_by_keep(captured)}
    assert "_context_overflow_recovery_rail" in kept
    assert "_llm_retry_rail" in kept
    assert "_context_processor_rail" in kept


def test_notifying_retry_rail_subclasses_sdk_llm_retry_rail():
    """工厂去重契约：宿主版必须是 SDK LLMRetryRail 的子类。

    create_deep_agent(enable_llm_retry_rail=True) 会注入基础版 LLMRetryRail，
    但对用户已提供的 rail 按 ``issubclass(t, LLMRetryRail)`` 去重
    （openjiuwen/harness/factory.py::_already_provided）。宿主版一旦不再继承
    SDK 基类，flash 挂宿主版后工厂会再注入一份基础版 → 双份重试循环。
    """
    assert issubclass(NotifyingLLMRetryRail, LLMRetryRail)


# ---------------------------------------------------------------------------
# builder 配置化：execution_guard.context_overflow_recovery.max_attempts
# ---------------------------------------------------------------------------


def test_recovery_builder_reads_max_attempts_from_config():
    rail = JiuwenSwarmFlashAdapter._build_context_overflow_recovery_rail(
        {"execution_guard": {"context_overflow_recovery": {"max_attempts": 5}}}
    )
    assert isinstance(rail, ContextOverflowRecoveryRail)
    assert rail._max_recovery_attempts == 5


def test_recovery_builder_default_without_config():
    rail = JiuwenSwarmFlashAdapter._build_context_overflow_recovery_rail()
    assert isinstance(rail, ContextOverflowRecoveryRail)
    assert rail._max_recovery_attempts == 3


@pytest.mark.parametrize("bad", [0, -1, "abc", None])
def test_recovery_builder_falls_back_to_default_on_invalid(bad):
    rail = JiuwenSwarmFlashAdapter._build_context_overflow_recovery_rail(
        {"execution_guard": {"context_overflow_recovery": {"max_attempts": bad}}}
    )
    assert rail._max_recovery_attempts == 3


def test_llm_retry_builder_respects_enable_flag():
    """开关与 normal 同源（execution_guard.llm_retry_rail）；关闭时 builder 返回 None。"""
    rail = JiuwenSwarmFlashAdapter._build_llm_retry_rail(
        {"execution_guard": {"llm_retry_rail": {"enabled": True, "max_retries": 2}}}
    )
    assert isinstance(rail, NotifyingLLMRetryRail)
    assert rail.max_retries == 2
    disabled = JiuwenSwarmFlashAdapter._build_llm_retry_rail(
        {"execution_guard": {"llm_retry_rail": {"enabled": False}}}
    )
    assert disabled is None


# ---------------------------------------------------------------------------
# 恢复链行为：413 检测 → 强制压缩 → 重试 → 熔断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overflow_413_triggers_force_compact_and_retry():
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _ctx(
        exception=_OverflowError(_ANTHROPIC_413, status_code=413),
        context=SimpleNamespace(_processors=[processor]),
    )
    await rail.on_model_exception(ctx)
    assert processor.force_compact is True
    assert processor.overflow_threshold_override == int(80000 * _RATIO)
    assert ctx.consume_retry_request() is not None


@pytest.mark.asyncio
async def test_overflow_huawei_format_detected():
    """华为格式（无 status_code，纯关键词 + prompt length 正则）也能触发恢复。"""
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _ctx(
        exception=_OverflowError(_HUAWEI_OVERFLOW),
        context=SimpleNamespace(_processors=[processor]),
    )
    await rail.on_model_exception(ctx)
    assert processor.force_compact is True
    assert processor.overflow_threshold_override == int(80000 * _RATIO)
    assert ctx.consume_retry_request() is not None


def test_overflow_modelarts_400_format_detected():
    """ModelArts.81001（400 + 溢出关键词）被识别为溢出。"""
    exc = _OverflowError(
        "Error code: 400 - ModelArts.81001: context length exceeded", status_code=400
    )
    assert ContextOverflowRecoveryRail._is_context_overflow_error(exc) is True


@pytest.mark.asyncio
async def test_non_overflow_error_does_not_recover():
    """认证 / 参数类错误不是溢出：不触发压缩、不重试（明确失败）。"""
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    ctx = _ctx(
        exception=_OverflowError("Error code: 401 Unauthorized", status_code=401),
        context=SimpleNamespace(_processors=[processor]),
    )
    await rail.on_model_exception(ctx)
    assert processor.force_compact is None
    assert ctx.consume_retry_request() is None


@pytest.mark.asyncio
async def test_persistent_overflow_circuit_breaks_with_user_visible_error():
    """持续溢出：前 N 次有界重试，第 N+1 次熔断并推错误帧；熔断后计数清零。"""
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
    processor = _FakeFullCompactProcessor()
    session = _FakeSession()
    ctx = _ctx(
        exception=_OverflowError(_ANTHROPIC_413, status_code=413),
        context=SimpleNamespace(_processors=[processor]),
        session=session,
    )
    for _ in range(3):
        await rail.on_model_exception(ctx)
        assert ctx.consume_retry_request() is not None

    # 第 4 次连续溢出 → 熔断：不再重试，错误帧上屏
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is None
    assert len(session.streams) == 1
    frame = session.streams[0]
    assert frame.type == "error"
    assert frame.payload["error_type"] == "context_overflow_circuit_break"

    # 熔断清零计数：新请求的溢出重新开始一轮恢复（有界，非永久死亡）
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None


@pytest.mark.asyncio
async def test_model_success_resets_overflow_counter():
    """恢复后模型调用成功 → 计数清零，下次溢出重新满额恢复。"""
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=2)
    processor = _FakeFullCompactProcessor()
    ctx = _ctx(
        exception=_OverflowError(_ANTHROPIC_413, status_code=413),
        context=SimpleNamespace(_processors=[processor]),
    )
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None

    ctx.exception = None
    await rail.after_model_call(ctx)  # 成功 → 清零

    ctx.exception = _OverflowError(_ANTHROPIC_413, status_code=413)
    await rail.on_model_exception(ctx)  # 重新从 1 计数，仍会重试
    assert ctx.consume_retry_request() is not None


@pytest.mark.asyncio
async def test_overflow_without_full_compact_processor_still_bounded():
    """桥接缺失（如部署关闭 context_engine）时：不崩溃、仍请求重试、按上限熔断。"""
    rail = ContextOverflowRecoveryRail(max_recovery_attempts=1)
    session = _FakeSession()
    ctx = _ctx(
        exception=_OverflowError(_ANTHROPIC_413, status_code=413),
        context=SimpleNamespace(_processors=[]),
        session=session,
    )
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None

    await rail.on_model_exception(ctx)  # 第 2 次 > 1 → 熔断
    assert ctx.consume_retry_request() is None
    assert session.streams
    assert session.streams[0].payload["error_type"] == "context_overflow_circuit_break"


# ---------------------------------------------------------------------------
# 重试 rail 行为：瞬时错误有界重试；非瞬时 / 取消直接传播
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_connection_error_retries_are_bounded():
    rail = _retry_rail(max_retries=2)
    ctx = _ctx(exception=Exception("Connection error."))
    await rail.before_invoke(ctx)

    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None
    await rail.on_model_exception(ctx)  # 预算用尽
    assert ctx.consume_retry_request() is None


@pytest.mark.asyncio
async def test_auth_error_is_not_retried():
    rail = _retry_rail()
    ctx = _ctx(exception=_OverflowError("Error code: 401 Unauthorized", status_code=401))
    await rail.before_invoke(ctx)
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is None


@pytest.mark.asyncio
async def test_cancelled_error_is_not_retried():
    """用户取消必须及时传播——不进入任何重试类别。"""
    rail = _retry_rail()
    ctx = _ctx(exception=asyncio.CancelledError())
    await rail.before_invoke(ctx)
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is None


@pytest.mark.asyncio
async def test_retry_notification_is_user_visible():
    """重试进行中对用户可见（retry_notification 帧），失败不静默。"""
    rail = NotifyingLLMRetryRail(
        max_retries=1,
        backoff_seconds=[0.0],
        notify_user_on_retry=True,
        notify_user_on_exhausted=True,
    )
    session = _FakeSession()
    ctx = _ctx(exception=Exception("Connection error."), session=session)
    await rail.before_invoke(ctx)
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None
    assert len(session.streams) == 1
    assert session.streams[0].type == "retry_notification"
