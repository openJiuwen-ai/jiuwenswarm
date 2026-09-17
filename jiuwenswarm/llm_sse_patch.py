# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime patch: make non-streaming OpenAI invoke() tolerate SSE-only gateways.

部分网关（如 celia-claw sse-api）即使在非流式调用下也只返回 ``text/event-stream``
文本，此时 openai SDK 交回给框架的 ``response`` 会是 ``str`` 而非 ``ChatCompletion``，
导致 ``response.choices`` 抛出 ``'str' object has no attribute 'choices'``，进而让
subagent / 心跳等走 ``invoke()`` 的非流式路径全部失败。

这里在服务启动时给 ``OpenAIModelClient._parse_response`` 打一个补丁：当收到 ``str``
响应时，先把 SSE 文本组装成标准的 ``ChatCompletion`` 再交回原解析逻辑。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("jiuwenswarm.llm_sse_patch")

_PATCH_APPLIED = False


def _parse_chunk(chunk_str: str) -> dict | None:
    """解析单个数据块 JSON。"""
    if not chunk_str or not chunk_str.startswith("data:"):
        return None
    try:
        return json.loads(chunk_str[5:].strip())
    except json.JSONDecodeError as e:
        logger.info("[ParserPatch] JSON 解析错误: %s", e)
        return None


def _extract_message_content(chunk: dict) -> tuple[str, str]:
    """从 chunk 中提取思考内容和输出内容。"""
    if not chunk or not chunk.get("choices"):
        return "", ""
    msg = chunk["choices"][0]["message"]
    return msg.get("reasoning_token_text", ""), msg.get("token_text", "")


def _build_tool_calls(msg: dict) -> list | None:
    """从消息中构建工具调用对象列表。"""
    if not msg.get("tool_calls"):
        return None
    from openai.types.chat import ChatCompletionMessageFunctionToolCall
    from openai.types.chat.chat_completion_message_function_tool_call import Function

    return [
        ChatCompletionMessageFunctionToolCall(
            id=tc["id"],
            type="function",
            function=Function(
                name=tc["function"]["name"],
                arguments=tc["function"]["arguments"],
            ),
        )
        for tc in msg["tool_calls"]
    ]


def assemble_openai_response(response: str) -> Any:
    """将分块 SSE 数据组装成标准的 OpenAI ``ChatCompletion``。"""
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.completion_usage import CompletionUsage

    content = think_content = ""
    last_chunk = None
    cache_chunk = ""

    for line in response.split("\n"):
        if not line.strip():
            continue
        if line.startswith("id:"):
            if cache_chunk:
                chunk = _parse_chunk(cache_chunk)
                cache_chunk = ""
                if chunk:
                    think, out = _extract_message_content(chunk)
                    think_content += think
                    content += out
                    last_chunk = chunk
        elif line.startswith("data:"):
            cache_chunk = line

    # 处理最后一个 chunk
    if cache_chunk:
        chunk = _parse_chunk(cache_chunk)
        if chunk:
            think, out = _extract_message_content(chunk)
            think_content += think
            content += out
            last_chunk = chunk

    # 提取并构建工具调用对象
    formatted_tool_calls = None
    if last_chunk and last_chunk.get("choices"):
        msg = last_chunk["choices"][0]["message"]
        formatted_tool_calls = _build_tool_calls(msg)

    # 构建 message（扩展 reasoning_content 字段存储思考内容）
    message = ChatCompletionMessage(
        role="assistant",
        content=content or None,
        tool_calls=formatted_tool_calls,
    )
    if think_content:
        message.reasoning_content = think_content

    # 构建 usage
    usage = None
    if last_chunk and last_chunk.get("usage"):
        u = last_chunk["usage"]
        usage = CompletionUsage(
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            total_tokens=u.get("total_tokens", 0),
        )

    # 构建 finish_reason：有工具调用时优先使用 "tool_calls"
    finish_reason = "stop"
    if formatted_tool_calls:
        finish_reason = "tool_calls"
    elif last_chunk and last_chunk.get("choices"):
        finish_reason = last_chunk["choices"][0].get("finish_reason", "stop")

    return ChatCompletion(
        id=last_chunk.get("id", "chatcmpl-default") if last_chunk else "chatcmpl-default",
        choices=[
            Choice(
                index=0,
                message=message,
                finish_reason=finish_reason,
            )
        ],
        created=int(time.time()),
        model="unknown",
        object="chat.completion",
        usage=usage,
    )


def apply_openai_sse_invoke_patch() -> None:
    """给 ``OpenAIModelClient._parse_response`` 打补丁以兼容 SSE-only 网关。

    幂等：重复调用只生效一次。在服务启动早期调用即可覆盖 subagent / 心跳等
    所有走非流式 ``invoke()`` 的 LLM 调用。
    """
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    try:
        from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
            OpenAIModelClient,
        )
    except Exception as exc:  # pragma: no cover - openjiuwen 不可用时静默跳过
        logger.warning("[llm_sse_patch] 未能导入 OpenAIModelClient，跳过补丁: %s", exc)
        return

    if getattr(OpenAIModelClient, "_sse_invoke_patch_applied", False):
        _PATCH_APPLIED = True
        return

    # monkeypatch 必须访问受保护成员 _parse_response 以包一层 SSE 兜底，
    # 属于运行期补丁的正常诉求，豁免 G.CLS.11 protected-access。
    _orig_parse_response = OpenAIModelClient._parse_response  # pylint: disable=protected-access

    async def _parse_response_with_sse_guard(
        self: Any,
        response: Any,
        parser: Optional[Any] = None,
    ):
        # 非标准 OpenAI 格式：SSE-only 网关在非流式调用下仍返回 str 文本，
        # 先组装成标准 ChatCompletion 再走原有解析逻辑。
        if isinstance(response, str):
            response = assemble_openai_response(response)
        return await _orig_parse_response(self, response, parser)

    # 同上：运行期替换方法 + 打幂等标记，需写受保护属性。
    OpenAIModelClient._parse_response = _parse_response_with_sse_guard  # pylint: disable=protected-access
    OpenAIModelClient._sse_invoke_patch_applied = True  # pylint: disable=protected-access
    _PATCH_APPLIED = True
    logger.info("[llm_sse_patch] OpenAIModelClient._parse_response SSE 兼容补丁已应用")
