# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import json
import time
import logging
from openai.types.chat import ChatCompletion, ChatCompletionMessage, ChatCompletionMessageFunctionToolCall
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_function_tool_call import Function
from openai.types.completion_usage import CompletionUsage

logger = logging.getLogger("jiuwenclaw.agentserver")


def _parse_chunk(chunk_str: str) -> dict | None:
    """解析单个数据块JSON"""
    if not chunk_str or not chunk_str.startswith("data:"):
        return None
    try:
        return json.loads(chunk_str[5:].strip())
    except json.JSONDecodeError as e:
        logger.info(f"[ParserPatch] JSON解析错误: {e}")
        return None


def _extract_message_content(chunk: dict) -> tuple[str, str]:
    """从chunk中提取思考内容和输出内容"""
    if not chunk or not chunk.get("choices"):
        return "", ""
    msg = chunk["choices"][0]["message"]
    return msg.get("reasoning_token_text", ""), msg.get("token_text", "")


def _build_tool_calls(msg: dict) -> list[ChatCompletionMessageFunctionToolCall] | None:
    """从消息中构建工具调用对象列表"""
    if not msg.get("tool_calls"):
        return None
    return [
        ChatCompletionMessageFunctionToolCall(
            id=tc.get("id", ""),
            type="function",
            function=Function(
                name=tc.get("function", {}).get("name", ""),
                arguments=tc.get("function", {}).get("arguments", "")
            )
        )
        for tc in msg["tool_calls"]
    ]


def assemble_openai_response(response: str) -> ChatCompletion:
    """
    将分块数据组装成标准的 OpenAI ChatCompletion 格式

    Returns:
        ChatCompletion
    """
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
        tool_calls=formatted_tool_calls
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
            total_tokens=u.get("total_tokens", 0)
        )

    # 构建 finish_reason：有工具调用时优先使用 "tool_calls"
    finish_reason = "stop"
    if formatted_tool_calls:
        finish_reason = "tool_calls"
    elif last_chunk and last_chunk.get("choices"):
        finish_reason = last_chunk["choices"][0].get("finish_reason", "stop")

    # 构建 ChatCompletion
    completion = ChatCompletion(
        id=last_chunk.get("id", "chatcmpl-default") if last_chunk else "chatcmpl-default",
        choices=[Choice(
            index=0,
            message=message,
            finish_reason=finish_reason
        )],
        created=int(time.time()),
        model="unknown",
        object="chat.completion",
        usage=usage
    )
    return completion
