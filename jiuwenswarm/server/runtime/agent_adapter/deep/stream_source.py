"""流帧来源与外层 ReAct 工具结果判定。

职责：
- 识别嵌套流（subagent / skill）镜像帧，避免污染父轮 bookkeeping
- 从 tool_result 嵌套结构读字段
- 区分外层 ReAct 与 SkillTurbo 内部工具结果
- 规范化 ask_user questions 键，供 HITL 活卡去重

纯判定函数，不触碰 adapter 实例状态。
"""

from __future__ import annotations

import json
from typing import Any


# 与 skill_turbo/executor.STREAM_SOURCE_ID_FIELD 一致：并发节点用它标识 source，
# 前端据其路由到 subagent 行。adapter 转发 llm_reasoning / llm_output 时须原样透传。
STREAM_SOURCE_ID_FIELD = "stream_source_id"

# SkillTurbo 内部工具 id 后缀（如 BashTool_skill_turbo）。外层 ReAct 工具结果不含此后缀。
_SKILL_TURBO_TOOL_ID_SUFFIX = "_skill_turbo"

def _is_foreign_stream_source(payload: Any) -> bool:
    """True when the frame was mirrored from a nested stream (subagent / skill node).

    The gateway fills a missing source with the ``"main"`` sentinel, so only a
    non-empty, non-``main`` id marks a frame that must not touch parent-turn
    bookkeeping.
    """
    if not isinstance(payload, dict):
        return False
    source_id = payload.get(STREAM_SOURCE_ID_FIELD)
    return isinstance(source_id, str) and bool(source_id) and source_id != "main"

def _tool_result_lookup(payload: Any, *keys: str) -> Any:
    """Read a field from a tool_result payload or its nested ``tool_result`` dict."""
    if not isinstance(payload, dict):
        return None
    nested = payload.get("tool_result")
    sources = (payload, nested) if isinstance(nested, dict) else (payload,)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None

def _is_outer_react_tool_result(payload: Any) -> bool:
    """True when this tool_result belongs to the outer ReAct agent, not SkillTurbo internals.

    Inner PPT/bash/read_file results carry ``stream_source_id`` or a tool id ending
    in ``_skill_turbo``. Resetting ``has_streamed_content`` on those would leak
    nested non-streamed answers as ``chat.final``.
    """
    source_id = _tool_result_lookup(payload, STREAM_SOURCE_ID_FIELD)
    if isinstance(source_id, str) and source_id.strip():
        return False
    tool_id = _tool_result_lookup(payload, "tool_call_id", "tool_id", "toolCallId")
    if isinstance(tool_id, str) and tool_id.strip().endswith(_SKILL_TURBO_TOOL_ID_SUFFIX):
        return False
    return True

def _ask_user_questions_key(payload: dict) -> str:
    """ask_user 卡片的 questions 规范化键，用于判定同一中断的重复通道。"""
    try:
        questions = payload.get("questions") or []
        return json.dumps(questions, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(payload.get("questions"))
