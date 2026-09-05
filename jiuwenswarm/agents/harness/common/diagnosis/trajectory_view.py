# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""将 Trajectory 规范化为有序步级视图的纯函数。

不依赖 rail 运行时；供归因器与诊断 rail 复用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from openjiuwen.agent_evolving.trajectory import (
    LLMCallDetail,
    ToolCallDetail,
    Trajectory,
    trajectory_steps,
)

# 覆盖 error/exception/failed/timeout/错误/异常/失败 等常见失败关键词
_FAILURE_KEYWORDS = re.compile(
    r"(?i)(\berror\b|\bexception\b|\bfailed\b|\bfailure\b|\btimeout\b|"
    r"错误|异常|失败|超时)",
)


@dataclass(frozen=True)
class StepView:
    """规范化后的单步视图（序号为返回序列中的 1-based 位置）。"""

    index: int
    kind: str
    tool_name: str
    arguments: dict
    result: str
    tool_call_id: str
    is_error: bool


def flatten_tool_calls(tool_calls: list) -> list[dict]:
    """将嵌套 ``function.*`` 拍平为统一扁平结构；已扁平或非 dict 原样返回。

    不就地修改入参。兼容框架契约不一致：
    - OpenAI 嵌套：``{"id","type","function":{"name","arguments"}}``
    - 扁平：``{"id","type","name","arguments"}``
    """
    out: list[dict] = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        func = tc.get("function")
        if isinstance(func, dict):
            out.append(
                {
                    "id": tc.get("id"),
                    "type": tc.get("type", "function"),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                    "index": tc.get("index"),
                    "response_item_id": tc.get("response_item_id"),
                }
            )
        else:
            out.append(tc)
    return out


def iter_tool_calls_from_message(message: Any) -> list[dict]:
    """从 dict 或对象型消息取出并拍平 ``tool_calls``；无则返回空列表。"""
    if isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    else:
        tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        return []
    if not isinstance(tool_calls, list):
        try:
            tool_calls = list(tool_calls)
        except TypeError:
            return []
    return flatten_tool_calls(tool_calls)


def _parse_arguments(call_args: Any) -> dict:
    """将 call_args 解析为 dict；JSON 字符串失败或非 dict 时返回 {}。"""
    if isinstance(call_args, dict):
        return dict(call_args)
    if isinstance(call_args, str):
        text = call_args.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _response_text(response: Any) -> str:
    """兼容 response 为对象（``.content``）、dict 或 str。"""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if content is not None:
        return content if isinstance(content, str) else str(content)
    if isinstance(response, dict):
        if "content" in response:
            c = response.get("content")
            return c if isinstance(c, str) else str(c)
        return json.dumps(response, ensure_ascii=False, default=str)
    return str(response)


def _is_error_result(result: str) -> bool:
    return bool(result) and bool(_FAILURE_KEYWORDS.search(result))


def build_step_view(
    trajectory: Trajectory,
    *,
    kinds: Sequence[str] = ("tool",),
) -> list[StepView]:
    """遍历 ``trajectory_steps``，按 ``kinds`` 过滤并生成连续 1-based 编号视图。"""
    allowed = set(kinds)
    views: list[StepView] = []
    index = 0
    for step in trajectory_steps(trajectory):
        kind = getattr(step, "kind", None)
        if kind not in allowed:
            continue
        detail = getattr(step, "detail", None)
        index += 1
        if kind == "tool":
            tool_name = ""
            arguments: dict = {}
            result = ""
            tool_call_id = ""
            if isinstance(detail, ToolCallDetail):
                tool_name = str(detail.tool_name or "")
                arguments = _parse_arguments(detail.call_args)
                result = "" if detail.call_result is None else str(detail.call_result)
                tool_call_id = str(detail.tool_call_id or "")
            elif isinstance(detail, dict):
                tool_name = str(detail.get("tool_name") or "")
                arguments = _parse_arguments(detail.get("call_args"))
                cr = detail.get("call_result")
                result = "" if cr is None else str(cr)
                tool_call_id = str(detail.get("tool_call_id") or "")
            views.append(
                StepView(
                    index=index,
                    kind="tool",
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                    tool_call_id=tool_call_id,
                    is_error=_is_error_result(result),
                )
            )
        elif kind == "llm":
            result = ""
            if isinstance(detail, LLMCallDetail):
                result = _response_text(detail.response)
            elif isinstance(detail, dict):
                result = _response_text(detail.get("response"))
            views.append(
                StepView(
                    index=index,
                    kind="llm",
                    tool_name="",
                    arguments={},
                    result=result,
                    tool_call_id="",
                    is_error=_is_error_result(result),
                )
            )
        else:
            # 未知 kind：仍占位编号，字段取保守默认值
            result = str(detail) if detail is not None else ""
            views.append(
                StepView(
                    index=index,
                    kind=str(kind),
                    tool_name="",
                    arguments={},
                    result=result,
                    tool_call_id="",
                    is_error=_is_error_result(result),
                )
            )
    return views


def render_steps(
    steps: Iterable[StepView],
    *,
    max_result_chars: int = 300,
) -> str:
    """渲染成给 LLM 看的编号文本。

    形如 ``[Step 3] calculator({"x":1}) -> result_preview``；超长截断并标注。
    """
    lines: list[str] = []
    for step in steps:
        args_json = json.dumps(step.arguments, ensure_ascii=False, default=str)
        preview = step.result or ""
        truncated = False
        if max_result_chars >= 0 and len(preview) > max_result_chars:
            preview = preview[:max_result_chars]
            truncated = True
        if step.kind == "tool":
            name = step.tool_name or "tool"
            line = f"[Step {step.index}] {name}({args_json}) -> {preview}"
        else:
            line = f"[Step {step.index}] llm() -> {preview}"
        if truncated:
            line += " ...[truncated]"
        lines.append(line)
    return "\n".join(lines)
