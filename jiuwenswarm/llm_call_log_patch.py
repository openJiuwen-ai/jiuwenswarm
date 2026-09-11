# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime patch: 打印传给大模型的 messages、模型返回的 reasoning_content
（思考内容）、模型发起的 tool_calls，以及工具执行结果。

一处覆盖所有模型调用（主对话 DeepAgent 循环、subagent、心跳、
/btw、recap、repair、compact、symphony 等），因为它们最终都经过
``openjiuwen.core.foundation.llm.Model`` 这一层。

日志走 ``logging.getLogger("jiuwenswarm.llm_call_log")``，输出到控制台
及 ``~/.jiuwenswarm/agent/.logs/`` 下的 gateway.log / full.log。

输出标签：
- ``[LLM invoke]`` / ``[LLM stream]`` —— 调用前的 messages。
- ``[LLM invoke reasoning]`` / ``[LLM stream reasoning]`` —— 返回的思考内容
  （非流式为完整返回；流式为各 chunk 累积后整体输出，避免碎片刷屏）。
- ``[LLM invoke tool_calls]`` / ``[LLM stream tool_calls]`` —— 模型发起的
  工具调用（name / arguments / id）。
- ``[Tool exec]`` / ``[Tool exec result]`` —— 工具执行入参与执行结果
  （覆盖 tool / workflow / subagent 全部工具类型，汇聚于
  ``AbilityManager._execute_single_tool_call``）。

环境变量：
- ``JIUWENSWARM_LLM_CALL_LOG`` —— ``0`` / ``false`` 关闭，默认开。
- ``JIUWENSWARM_LLM_CALL_LOG_MAXLEN`` —— 单条 content 截断长度，``0`` 表示不截断，
  默认 ``8000``。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("jiuwenswarm.llm_call_log")

_PATCH_APPLIED = False


def _is_disabled() -> bool:
    """环境变量是否显式关闭日志。"""
    val = os.environ.get("JIUWENSWARM_LLM_CALL_LOG", "1").strip().lower()
    return val in ("0", "false", "no", "off")


def _max_content_len() -> int:
    """单条 content 的截断长度，0 表示不截断。"""
    raw = os.environ.get("JIUWENSWARM_LLM_CALL_LOG_MAXLEN", "8000").strip()
    try:
        v = int(raw)
    except ValueError:
        return 8000
    return max(0, v)


def _truncate(text: str) -> str:
    limit = _max_content_len()
    if limit and len(text) > limit:
        return text[:limit] + f"...<+{len(text) - limit} chars>"
    return text


def _content_to_str(content: Any) -> str:
    """把单条消息的 content 转成可读字符串。"""
    if content is None:
        return "<none>"
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - 防御性兜底
        return repr(content)


def _describe_messages(messages: Any) -> str:
    """把 messages 描述成多行可读文本。"""
    if messages is None:
        return "(none)"
    if isinstance(messages, str):
        return f"[str] {_truncate(messages)}"
    if not isinstance(messages, (list, tuple)):
        return f"[{type(messages).__name__}] {_truncate(repr(messages))}"

    lines: list[str] = []
    for i, msg in enumerate(messages):
        if isinstance(msg, dict):
            role = msg.get("role", "?")
            content = msg.get("content")
        else:
            role = getattr(msg, "role", None) or getattr(msg, "type", None) or "?"
            content = getattr(msg, "content", None)
            if content is None:
                # ToolMessage / 结构化消息可能用别的字段承载载荷
                content = (
                    getattr(msg, "tool_call_id", None)
                    or getattr(msg, "arguments", None)
                    or msg
                )
        lines.append(
            f"  [{i}] role={role} content={_truncate(_content_to_str(content))}"
        )
    return "\n".join(lines)


def _describe_tools(tools: Any) -> str:
    if not tools:
        return "(none)"
    if isinstance(tools, (list, tuple)):
        names: list[str] = []
        for t in tools:
            name: Any = None
            if isinstance(t, dict):
                fn = t.get("function")
                name = fn.get("name") if isinstance(fn, dict) else None
                name = name or t.get("name")
            else:
                fn = getattr(t, "function", None)
                name = getattr(fn, "name", None) if fn is not None else None
                name = name or getattr(t, "name", None)
            names.append(str(name or "<anon>"))
        return f"[{len(tools)} tools: {', '.join(names)}]"
    return repr(tools)


def _model_name(self: Any, model: Any) -> str:
    if model:
        return str(model)
    cfg = getattr(self, "model_config", None)
    return str(
        getattr(cfg, "model_name", None)
        or getattr(cfg, "model", None)
        or "?"
    )


def _extract_tool_calls(result: Any) -> list[dict] | None:
    """从非流式返回结果里取模型发起的 tool_calls。

    返回 [{"name","arguments","id"}] 或 None（无工具调用时）。
    """
    tcs = getattr(result, "tool_calls", None)
    if tcs is None and isinstance(result, dict):
        tcs = result.get("tool_calls")
    if not tcs:
        return None
    out: list[dict] = []
    for tc in tcs:
        if isinstance(tc, dict):
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
            out.append({
                "name": (fn.get("name") if fn else None) or tc.get("name") or "<anon>",
                "arguments": _truncate(_content_to_str((fn.get("arguments") if fn else None) or tc.get("arguments"))),
                "id": tc.get("id"),
            })
        else:
            out.append({
                "name": getattr(tc, "name", None) or "<anon>",
                "arguments": _truncate(_content_to_str(getattr(tc, "arguments", None))),
                "id": getattr(tc, "id", None),
            })
    return out


def _describe_tool_calls(tool_calls: list[dict] | None) -> str:
    """把模型发起的 tool_calls 描述成多行文本。"""
    if not tool_calls:
        return "(none)"
    lines: list[str] = []
    for i, tc in enumerate(tool_calls):
        lines.append(
            f"  [{i}] name={tc.get('name')} id={tc.get('id')} "
            f"arguments={tc.get('arguments')}"
        )
    return "\n".join(lines)


def _extract_reasoning(result: Any) -> str | None:
    """从非流式返回结果里取 reasoning_content（思考内容）。

    兼容 AssistantMessage / dict / 其它对象。
    """
    if result is None:
        return None
    # AssistantMessage / chunk 等对象
    val = getattr(result, "reasoning_content", None)
    if val is not None:
        return val
    # dict 形态
    if isinstance(result, dict):
        return result.get("reasoning_content")
    return None


def _extract_chunk_reasoning(chunk: Any) -> str:
    """从流式 chunk 取本块增量的 reasoning_content。"""
    if chunk is None:
        return ""
    val = getattr(chunk, "reasoning_content", None)
    if val is None and isinstance(chunk, dict):
        val = chunk.get("reasoning_content")
    return val or ""


def apply_llm_call_log_patch() -> None:
    """给 ``Model.invoke`` / ``Model.stream`` 包一层入参日志。幂等。"""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    if _is_disabled():
        _PATCH_APPLIED = True
        logger.info("[llm_call_log] 已通过环境变量关闭")
        return

    try:
        from openjiuwen.core.foundation.llm.model import Model
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "[llm_call_log] 未能导入 openjiuwen Model，跳过日志补丁: %s", exc
        )
        return

    if getattr(Model, "_llm_call_log_applied", False):
        _PATCH_APPLIED = True
        return

    _orig_invoke = Model.invoke
    _orig_stream = Model.stream

    async def _invoke_with_log(  # type: ignore[no-untyped-def]
        self, messages, *, tools=None, model=None, **kw
    ):
        logger.info(
            "[LLM invoke] sls model=%s tools=%s\n%s",
            _model_name(self, model),
            _describe_tools(tools),
            _describe_messages(messages),
        )
        result = await _orig_invoke(self, messages, tools=tools, model=model, **kw)
        reasoning = _extract_reasoning(result)
        if reasoning:
            logger.info(
                "[LLM invoke reasoning] model=%s\n%s",
                _model_name(self, model),
                _truncate(reasoning),
            )
        tool_calls = _extract_tool_calls(result)
        if tool_calls:
            logger.info(
                "[LLM invoke tool_calls] model=%s\n%s",
                _model_name(self, model),
                _describe_tool_calls(tool_calls),
            )
        return result

    async def _stream_with_log(  # type: ignore[no-untyped-def]
        self, messages, *, tools=None, model=None, **kw
    ):
        logger.info(
            "[LLM stream] sls model=%s tools=%s\n%s",
            _model_name(self, model),
            _describe_tools(tools),
            _describe_messages(messages)
        )
        reasoning_buf: list[str] = []
        tool_calls_acc: Any = None  # 累积合并后的 AssistantMessageChunk
        async for chunk in _orig_stream(self, messages, tools=tools, model=model, **kw):
            piece = _extract_chunk_reasoning(chunk)
            if piece:
                reasoning_buf.append(piece)
            # 利用 chunk 的 __add__ 合并 tool_calls 增量
            if getattr(chunk, "tool_calls", None):
                tool_calls_acc = chunk if tool_calls_acc is None else (tool_calls_acc + chunk)
            yield chunk
        if reasoning_buf:
            logger.info(
                "[LLM stream reasoning] model=%s\n%s",
                _model_name(self, model),
                _truncate("".join(reasoning_buf)),
            )
        tool_calls = _extract_tool_calls(tool_calls_acc)
        if tool_calls:
            logger.info(
                "[LLM stream tool_calls] model=%s\n%s",
                _model_name(self, model),
                _describe_tool_calls(tool_calls),
            )

    Model.invoke = _invoke_with_log  # type: ignore[assignment]
    Model.stream = _stream_with_log  # type: ignore[assignment]
    Model._llm_call_log_applied = True  # type: ignore[attr-defined]
    _PATCH_APPLIED = True
    logger.info("[llm_call_log] Model.invoke / Model.stream 日志补丁已应用")

    _apply_ability_manager_tool_result_patch()


def _apply_ability_manager_tool_result_patch() -> None:
    """给 ``AbilityManager._execute_single_tool_call`` 包一层工具结果日志。

    这是 openjiuwen 内所有单个工具执行（tool / workflow / subagent）的统一汇聚点，
    在返回前打印工具名、入参、执行结果（``ToolMessage.content``）。
    幂等。
    """
    try:
        from openjiuwen.core.single_agent.ability_manager import AbilityManager
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "[llm_call_log] 未能导入 AbilityManager，跳过工具结果日志补丁: %s", exc
        )
        return

    if getattr(AbilityManager, "_llm_call_log_tool_applied", False):
        return

    _orig_exec = AbilityManager._execute_single_tool_call

    async def _exec_with_log(  # type: ignore[no-untyped-def]
        self, tool_call, session, *args, **kwargs
    ):
        name = getattr(tool_call, "name", None) or "<anon>"
        args_str = _truncate(_content_to_str(getattr(tool_call, "arguments", None)))
        logger.info(
            "[Tool exec] name=%s\n  arguments=%s",
            name,
            args_str,
        )
        try:
            result, tool_message = await _orig_exec(
                self, tool_call, session, *args, **kwargs
            )
        except Exception:
            logger.info("[Tool exec result] name=%s ERROR", name)
            raise
        content = _truncate(_content_to_str(getattr(tool_message, "content", None)))
        logger.info(
            "[Tool exec result] name=%s\n  result=%s",
            name,
            content,
        )
        return result, tool_message

    AbilityManager._execute_single_tool_call = _exec_with_log  # type: ignore[assignment]
    AbilityManager._llm_call_log_tool_applied = True  # type: ignore[attr-defined]
    logger.info(
        "[llm_call_log] AbilityManager._execute_single_tool_call 工具结果日志补丁已应用"
    )
