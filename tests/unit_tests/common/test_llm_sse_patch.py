# -*- coding: utf-8 -*-
"""llm_sse_patch 单元测试。

该模块此前零测试覆盖（jiuwenswarm/llm_sse_patch.py，唯一消费点为
server/app_agentserver.py 在 channels.xiaoyi.mode == "xiaoyi_claw" 时应用的
SSE 兜底补丁）。用例覆盖：

- SSE 组装：id/data 交替、标准无 id 事件流、[DONE] 结尾不丢尾块；
- chunk 形态：message / delta 双形态，方言字段 token_text 与标准字段
  content 双字段名；
- 提取逻辑：usage / finish_reason / tool_calls 回归；
- 纯函数：_parse_chunk / _extract_message_content 边界。

需要 openai 的组装用例在 openai 缺失时跳过；纯函数用例无外部依赖。
"""

from __future__ import annotations

import json

import pytest

import jiuwenswarm.llm_sse_patch as sse_patch
from jiuwenswarm.llm_sse_patch import _extract_message_content, _parse_chunk

try:
    import openai  # noqa: F401

    HAS_OPENAI = True
except ImportError:  # pragma: no cover - 本地裁剪环境可能未装
    HAS_OPENAI = False

requires_openai = pytest.mark.skipif(not HAS_OPENAI, reason="openai 未安装")


def _chunk(
    text: str = "",
    *,
    reasoning: str = "",
    finish_reason: str | None = None,
    usage: dict | None = None,
    delta: bool = False,
) -> str:
    """构造一条标准 SSE data 行的 JSON 载荷。"""
    key = "delta" if delta else "message"
    payload: dict = {
        "choices": [{key: {"token_text": text, "reasoning_token_text": reasoning}}]
    }
    if finish_reason is not None:
        payload["choices"][0]["finish_reason"] = finish_reason
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload, ensure_ascii=False)


def _chunk_std(
    content: str = "",
    *,
    reasoning: str = "",
    delta: bool = False,
    finish_reason: str | None = None,
) -> str:
    """构造标准字段（content/reasoning_content）形态的 data 行载荷。"""
    key = "delta" if delta else "message"
    payload: dict = {
        "choices": [{key: {"content": content, "reasoning_content": reasoning}}]
    }
    if finish_reason is not None:
        payload["choices"][0]["finish_reason"] = finish_reason
    return json.dumps(payload, ensure_ascii=False)


# ---------- 纯函数 ----------


def test_parse_chunk_valid_and_invalid() -> None:
    assert _parse_chunk('data: {"a": 1}') == {"a": 1}
    assert _parse_chunk('data:  {"a": 1}') == {"a": 1}  # 冒号后空格
    assert _parse_chunk("data: [DONE]") is None  # 非 JSON → None
    assert _parse_chunk("event: ping") is None  # 非 data 前缀
    assert _parse_chunk("") is None
    assert _parse_chunk("data: {broken json") is None


def test_extract_message_content_dialect_fields() -> None:
    chunk = {
        "choices": [{"message": {"token_text": "输出", "reasoning_token_text": "思考"}}]
    }
    assert _extract_message_content(chunk) == ("思考", "输出")


def test_extract_message_content_standard_and_delta_forms() -> None:
    msg_form = {"choices": [{"message": {"content": "c1", "reasoning_content": "r1"}}]}
    assert _extract_message_content(msg_form) == ("r1", "c1")
    delta_form = {"choices": [{"delta": {"content": "c2", "reasoning_content": "r2"}}]}
    assert _extract_message_content(delta_form) == ("r2", "c2")


def test_extract_message_content_missing_keys_do_not_raise() -> None:
    assert _extract_message_content({}) == ("", "")
    assert _extract_message_content({"choices": []}) == ("", "")
    delta_only = {"choices": [{"delta": {"content": "d"}}]}
    assert _extract_message_content(delta_only) == ("", "d")
    no_message = {"choices": [{"finish_reason": "stop"}]}
    assert _extract_message_content(no_message) == ("", "")


# ---------- SSE 组装（需 openai） ----------


@requires_openai
def test_assemble_id_data_interleaved_with_done_keeps_last_chunk() -> None:
    """id/data 交替 + [DONE] 结尾：最后一个真实 chunk 不得被丢弃。"""
    stream = (
        f"id: 1\ndata: {_chunk('第一')}\n"
        f"id: 2\ndata: {_chunk('第二')}\n"
        f"id: 3\ndata: {_chunk('第三', finish_reason='length', usage={'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3})}\n"
        "data: [DONE]\n\n"
    )
    result = sse_patch.assemble_openai_response(stream)
    assert result.choices[0].message.content == "第一第二第三"
    assert result.choices[0].finish_reason == "length"
    assert result.usage is not None
    assert result.usage.total_tokens == 3


@requires_openai
def test_assemble_standard_sse_without_id_lines() -> None:
    """标准 SSE（空行分隔、无 id: 行）：每个 data 事件都要参与组装。"""
    stream = f"data: {_chunk('甲')}\n\ndata: {_chunk('乙')}\n\n"
    result = sse_patch.assemble_openai_response(stream)
    assert result.choices[0].message.content == "甲乙"


@requires_openai
def test_assemble_delta_form_no_keyerror() -> None:
    """delta 形态 chunk：不得抛 KeyError，内容正常提取。"""
    stream = (
        f"data: {_chunk('片段一', delta=True)}\n"
        f"data: {_chunk_std('片段二', delta=True, finish_reason='stop')}\n"
        "data: [DONE]\n"
    )
    result = sse_patch.assemble_openai_response(stream)
    assert result.choices[0].message.content == "片段一片段二"
    assert result.choices[0].finish_reason == "stop"


@requires_openai
def test_assemble_standard_content_field() -> None:
    """标准 message.content / reasoning_content 字段：不得组装出空回复。"""
    stream = (
        f"data: {_chunk_std('你好', reasoning='想了想')}\n"
        f"data: {_chunk_std('，世界', finish_reason='stop')}\n"
        "data: [DONE]\n"
    )
    result = sse_patch.assemble_openai_response(stream)
    assert result.choices[0].message.content == "你好，世界"
    assert result.choices[0].message.reasoning_content == "想了想"


@requires_openai
def test_assemble_dialect_fields_regression() -> None:
    """方言字段（token_text/reasoning_token_text）原行为保持。"""
    stream = f"data: {_chunk('答案', reasoning='推导')}\ndata: [DONE]\n"
    result = sse_patch.assemble_openai_response(stream)
    assert result.choices[0].message.content == "答案"
    assert result.choices[0].message.reasoning_content == "推导"


@requires_openai
def test_assemble_usage_finish_reason_defaults() -> None:
    """无 finish_reason/usage 时回退 stop / None，不抛异常。"""
    stream = f"data: {_chunk('仅内容')}\n\n"
    result = sse_patch.assemble_openai_response(stream)
    assert result.choices[0].message.content == "仅内容"
    assert result.choices[0].finish_reason == "stop"
    assert result.usage is None
    assert result.model == "unknown"
    assert result.object == "chat.completion"


@requires_openai
def test_assemble_tool_calls_message_and_delta_forms() -> None:
    """tool_calls 提取：message 形态保持，delta 形态不抛 KeyError。"""
    tc = [{"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}]
    msg_stream = (
        "data: "
        + json.dumps(
            {"choices": [{"message": {"token_text": "", "tool_calls": tc}}]},
            ensure_ascii=False,
        )
        + "\ndata: [DONE]\n"
    )
    result = sse_patch.assemble_openai_response(msg_stream)
    assert result.choices[0].finish_reason == "tool_calls"
    assert result.choices[0].message.tool_calls[0].function.name == "get_weather"

    delta_stream = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"content": "", "tool_calls": tc}}]},
            ensure_ascii=False,
        )
        + "\ndata: [DONE]\n"
    )
    result2 = sse_patch.assemble_openai_response(delta_stream)  # 修复前此处 KeyError
    assert result2.choices[0].finish_reason == "tool_calls"


@requires_openai
def test_assemble_garbage_and_empty_input() -> None:
    """空串 / 纯 [DONE] / 非 JSON 行：不抛异常，返回空内容补全对象。"""
    for stream in ("", "\n\n", "data: [DONE]\n", "event: ping\ndata: not-json\n"):
        result = sse_patch.assemble_openai_response(stream)
        assert result.choices[0].message.content is None
        assert result.choices[0].finish_reason == "stop"
