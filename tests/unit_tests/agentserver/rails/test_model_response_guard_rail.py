# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ModelResponseGuardRail 单元测试.

覆盖 mf_002（SSE 中途断连）/ mf_006（空响应）两类缺陷守护场景，
以及图片降级豁免、失败尝试跳过、非流式跳过等边界。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.execution_guard import (
    ModelResponseGuardRail,
)


def _make_ctx(
    response,
    *,
    streaming: bool = True,
    messages=None,
    exception=None,
    retry_attempt: int = 0,
):
    extra = {"_streaming": True} if streaming else {}
    return SimpleNamespace(
        retry_attempt=retry_attempt,
        extra=extra,
        exception=exception,
        inputs=SimpleNamespace(response=response, messages=messages or []),
    )


def _message(content="", *, reasoning="", tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls if tool_calls is not None else [],
        finish_reason=finish_reason,
    )


@pytest.fixture
def rail():
    return ModelResponseGuardRail()


async def test_incomplete_stream_with_partial_content_raises(rail):
    """mf_002 场景：SSE 中途断连，截断残句 + 无 finish_reason → 必须抛错."""
    ctx = _make_ctx(_message(content="答案是 答案是 "))
    await rail.before_model_call(ctx)
    with pytest.raises(Exception, match="finish_reason"):
        await rail.after_model_call(ctx)


async def test_incomplete_stream_without_output_raises(rail):
    """断连且零产出：按流不完整上报（优先于空响应判定）."""
    ctx = _make_ctx(_message(content=""))
    await rail.before_model_call(ctx)
    with pytest.raises(Exception, match="finish_reason"):
        await rail.after_model_call(ctx)


async def test_empty_response_with_clean_stop_raises(rail):
    """mf_006 场景：协议完整（finish_reason=stop）但零产出 → 必须抛错."""
    ctx = _make_ctx(_message(content="", finish_reason="stop"))
    await rail.before_model_call(ctx)
    with pytest.raises(Exception, match="空响应"):
        await rail.after_model_call(ctx)


async def test_normal_completion_passes(rail):
    ctx = _make_ctx(_message(content="计算结果为 8265", finish_reason="stop"))
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)


async def test_tool_calls_only_passes(rail):
    ctx = _make_ctx(
        _message(content="", finish_reason="tool_calls", tool_calls=[object()])
    )
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)


async def test_reasoning_only_passes(rail):
    ctx = _make_ctx(_message(content="", reasoning="思考中", finish_reason="stop"))
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)


async def test_inspector_finish_reason_evidence_prevents_false_raise(rail):
    """流中出现过 finish_reason（哪怕 response 聚合丢失）不误判为流不完整."""
    ctx = _make_ctx(_message(content="部分输出"))
    await rail.before_model_call(ctx)
    await rail.inspect_stream_chunk(ctx, SimpleNamespace(finish_reason="stop"))
    await rail.after_model_call(ctx)


async def test_image_input_fallback_exempt(rail):
    """图片降级提示消息无 finish_reason 属优雅降级，不得误伤."""
    image_msg = SimpleNamespace(
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]
    )
    ctx = _make_ctx(
        _message(content="当前主模型或模型服务端点不支持图片输入，请切换到支持视觉输入的主模型"),
        messages=[image_msg],
    )
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)


async def test_failed_attempt_skipped(rail):
    """调用本身已失败（重试/上抛路径）时 guard 不重复判定."""
    ctx = _make_ctx(_message(content="", finish_reason=None), exception=RuntimeError("boom"))
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)


async def test_non_streaming_skips_incomplete_check(rail):
    """非流式响应不做流完整性判定（finish_reason 已在解析层规范化）."""
    ctx = _make_ctx(_message(content="普通回答"), streaming=False)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)


async def test_retry_attempt_keeps_inspector_state(rail):
    """重试 attempt 不重置探针状态，避免重试轮被误判（与 LLMRetryRail 口径一致）."""
    ctx = _make_ctx(_message(content="重试后的部分输出"), retry_attempt=1)
    await rail.before_model_call(ctx)
    await rail.inspect_stream_chunk(ctx, SimpleNamespace(finish_reason="stop"))
    ctx2 = _make_ctx(_message(content="x"), retry_attempt=1)
    await rail.before_model_call(ctx2)
    # attempt==1 不重置 → saw_finish_reason 仍为 True（实例级）
    assert rail._saw_finish_reason is True


def test_builder_default_enabled(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as idm

    monkeypatch.setattr(idm, "get_config", lambda: {})
    rail = idm.JiuWenSwarmDeepAdapter._build_model_response_guard_rail(object())
    assert isinstance(rail, ModelResponseGuardRail)
    assert rail.check_incomplete_stream is True
    assert rail.check_empty_response is True


def test_builder_can_be_disabled(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as idm

    monkeypatch.setattr(
        idm,
        "get_config",
        lambda: {"execution_guard": {"model_response_guard": {"enabled": False}}},
    )
    rail = idm.JiuWenSwarmDeepAdapter._build_model_response_guard_rail(object())
    assert rail is None
