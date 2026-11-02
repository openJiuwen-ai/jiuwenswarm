"""上下文压缩、recap 与部分 compact。"""

from __future__ import annotations

import logging
import re
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.deep.model_factory import (
    _build_messages_for_model,
)

logger = logging.getLogger(__name__)


async def compress_context(
        adapter,
        session_id: str,
        session: Any = None,
        *,
        return_state: bool = False,
) -> dict[str, Any]:
    """主动触发上下文压缩。

    Args:
        session_id: 会话ID
        session: Session 对象（可选）

    Returns:
        包含压缩结果的字典:
        - result: "busy" | "compressed" | "noop"
        - stats: 压缩统计信息（仅当 result == "compressed" 时）
    """
    if not adapter._is_session_scoped_adapter:
        session_adapter = await adapter._get_or_create_session_adapter(session_id)
        try:
            return await session_adapter.compress_context(
                session_id=session_id,
                session=session,
                return_state=return_state,
            )
        finally:
            await adapter._evict_idle_session_adapters()

    if adapter._instance is None or adapter._instance.react_agent is None:
        raise ValueError("Agent instance not available")

    context_engine = adapter._instance.react_agent.context_engine
    react_agent = adapter._instance.react_agent

    context = context_engine.get_context(session_id=session_id)
    if context is None:
        return {"result": "noop", "stats": None}

    raw_total_tokens = await adapter._count_full_context_tokens(
        context, react_agent, session_id
    )

    compact_result = await context_engine.compress_context(
        session=session,
        session_id=session_id,
        return_state=True,
    )
    summary: str | None = None
    state: dict[str, Any] | None = None
    if isinstance(compact_result, dict):
        result = compact_result.get("result") or compact_result.get("status")
        raw_state = compact_result.get("state")
        if isinstance(raw_state, dict):
            state = raw_state
        raw_summary = compact_result.get("compact_summary")
        if raw_summary is None:
            if isinstance(state, dict):
                raw_summary = state.get("compact_summary")
        if isinstance(raw_summary, str) and raw_summary.strip():
            summary = raw_summary.strip()
    else:
        result = compact_result

    response: dict[str, Any] = {"result": result}
    if return_state and state:
        response["state"] = state
        compact_summary = state.get("compact_summary")
        if isinstance(compact_summary, str) and compact_summary.strip():
            response["compact_summary"] = compact_summary.strip()

    if result == "compressed":
        context = context_engine.get_context(session_id=session_id)
        if context:
            total_tokens = await adapter._count_full_context_tokens(
                context, react_agent, session_id
            )

            stats = context.statistic()
            response["stats"] = {
                "total_messages": stats.total_messages,
                "total_tokens": total_tokens,
                "raw_total_tokens": raw_total_tokens,
            }
            if summary:
                response["summary"] = summary
                response.setdefault("compact_summary", summary)

    return response


async def generate_recap(
    adapter,
    session_id: str,
    request: AgentRequest | None = None,
) -> dict[str, Any]:
    """生成会话快速回顾（read-only，不修改对话历史）。

    取最近30条消息 → fast model → 1-3句摘要。
    """
    if not adapter._is_session_scoped_adapter:
        session_adapter = await adapter._get_or_create_session_adapter(
            session_id,
            request=request,
        )
        try:
            return await session_adapter.generate_recap(
                session_id=session_id,
                request=request,
            )
        finally:
            await adapter._evict_idle_session_adapters()

    from jiuwenswarm.server.runtime.agent_adapter.recap_prompts import (
        RECENT_MESSAGE_WINDOW,
        build_recap_prompt,
    )

    messages = adapter._get_recent_messages(session_id, window=RECENT_MESSAGE_WINDOW)
    if not messages:
        return {"status": "no_turn"}

    # 透传主 agent tools schema 保 cache key（工具执行由单轮 + tool_use 丢弃禁止）
    tools = await adapter._get_agent_tools(session_id)

    prompt = build_recap_prompt(memory=None, language=adapter._resolve_prompt_language())
    summary_text = await adapter._call_model_for_recap(messages, prompt, tools=tools or None)
    if not summary_text:
        return {"status": "failed", "error": "Model returned empty response"}

    return {"status": "ok", "summary": summary_text.strip()}


async def _call_model_for_recap(
    adapter,
    messages: list[Any],
    prompt: str,
    system_prompt: str = "",
    enable_prompt_caching: bool = True,
    tools: list[Any] | None = None,
) -> str | None:
    """调用 model 生成简短回答（单轮、禁工具执行）。

    - system_prompt 非空时以 SystemMessage 形式前置
    - prompt 作为最后一条 user message 追加到对话末尾
    - tools 非空时透传给模型以保 cache key（与主 agent 一致），但单轮 +
      tool_use 检测丢弃 = 工具不被执行（对齐 claude-code canUseTool:deny）
    - 不设置 temperature（继承模型默认值，与主 agent 保持一致以复用 prompt cache）

    prompt cache 策略：
    - 保持消息原始格式（保留 structured content blocks，包括 tool_use/tool_result）
    - 最后一条 pre-prompt 消息添加 cache_control: {type: "ephemeral"} marker
    - btw prompt 不添加 cache_control（skipCacheWrite — 侧问题响应不写入 cache）
    """

    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _try_add_cache_control,
    )

    from openjiuwen.core.foundation.llm.schema.message import (
        AssistantMessage,
        SystemMessage,
        UserMessage,
    )

    if adapter._model is None:
        logger.error("[oneshot] no model instance available")
        return None

    recap_messages: list[Any] = []

    if system_prompt:
        recap_messages.append(SystemMessage(content=system_prompt))

    for msg in messages:
        role = getattr(msg, "role", None) or ""
        content = getattr(msg, "content", None) or ""

        # Skip truly empty messages
        if isinstance(content, str) and not content.strip():
            continue
        if isinstance(content, (list, tuple)) and len(content) == 0:
            continue
        if content is None:
            continue

        # Keep original content format (string or list of structured blocks).
        # This is critical for prompt cache prefix matching — converting to
        # plain text with str() would strip tool_use/tool_result blocks and
        # break byte-identical prefix matching with the main agent's calls.
        if role == "user":
            recap_messages.append(UserMessage(content=content))
        elif role == "assistant":
            recap_messages.append(AssistantMessage(content=content))
        else:
            recap_messages.append(UserMessage(content=content))

    # Mark the last pre-prompt message for prompt caching.
    # The btw prompt itself does NOT carry a cache_control marker
    # skipCacheWrite — the side-question
    # response doesn't create a new cache entry.
    if enable_prompt_caching and recap_messages:
        _try_add_cache_control(recap_messages[-1])

    # Append btw prompt as final user message (no cache_control → skipCacheWrite)
    recap_messages.append(UserMessage(content=prompt))

    try:
        # No temperature override — inherit model default to match main agent
        # API params (thinking config is part of the Anthropic cache key).
        result = await adapter._model.invoke(recap_messages, tools=tools)
        # Tool-use guard: tools schema is passed only to preserve the cache
        # key (matches the main agent). Single turn + discard any tool_use
        # the model emits → tools are never executed. Aligned with
        # claude-code's canUseTool:{behavior:'deny'} + tool_use fallback.
        tool_calls = getattr(result, "tool_calls", None)
        if tool_calls:
            names = ", ".join(getattr(tc, "name", "tool") for tc in tool_calls)
            logger.info(
                "[btw/recap] model emitted tool_use despite no-tool constraint: %s",
                names,
            )
            return (
                f"(模型尝试调用工具 {names} 而非直接回答。"
                "请重新措辞或在主对话中提问。)"
            )
        content = getattr(result, "content", None) or str(result)
        # Log cache metrics for observability
        usage = getattr(result, "usage_metadata", None)
        if usage and getattr(usage, "cache_tokens", 0) > 0:
            logger.info(
                "[btw/recap] cache hit: cache_tokens=%s, input_tokens=%s, output_tokens=%s",
                getattr(usage, "cache_tokens", 0),
                getattr(usage, "input_tokens", 0),
                getattr(usage, "output_tokens", 0),
            )
        return content
    except Exception:
        logger.exception("[generate_recap] model call failed")
        return None


async def compact_partial(
    adapter,
    session_id: str,
    turn_index: int,
    direction: str = "from",
) -> dict[str, Any]:
    """部分对话压缩 — /rewind summarize from here 的核心实现。

    从 history.json 读取消息，找到指定 turn 的 pivot 位置，将对应范围的消息
    发送给 LLM 生成结构化摘要（9 节：Primary Request, Technical Concepts,
    Files, Errors, Problem Solving, User Messages, Pending Tasks,
    Current Work, Optional Next Step）。

    Args:
        session_id: 会话ID
        turn_index: 基准 turn 号（1-based）
        direction: "from" (摘要 turn 及之后) 或 "up_to" (摘要 turn 之前)

    Returns:
        - status: "ok" | "no_turn" | "failed"
        - summary: 摘要文本
        - summarized_count: 被摘要的消息 record 数
    """
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        get_agent_sessions_dir,
    )
    from jiuwenswarm.server.runtime.session.session_history import _read_history
    from jiuwenswarm.server.runtime.agent_adapter.compact_partial_prompts import (
        NO_TOOLS_PREAMBLE,
        PARTIAL_COMPACT_PROMPT,
        PARTIAL_COMPACT_UP_TO_PROMPT,
    )

    sessions_dir = get_agent_sessions_dir()
    history_path = sessions_dir / session_id / "history.json"
    history = _read_history(history_path)
    if not history:
        return {"status": "no_turn"}

    user_positions = []
    for i, record in enumerate(history):
        if record.get("role") == "user":
            user_positions.append(i)

    total_turns = len(user_positions)
    if total_turns == 0 or turn_index > total_turns:
        return {"status": "no_turn"}

    pivot_idx = user_positions[turn_index - 1]

    if direction == "from":
        messages_to_summarize = history[pivot_idx:]
    elif direction == "up_to":
        messages_to_summarize = history[:pivot_idx]
    else:
        return {"status": "failed", "error": f"unknown direction: {direction}"}

    if not messages_to_summarize:
        return {"status": "no_turn"}

    summarized_count = len(messages_to_summarize)

    prompt = (
        NO_TOOLS_PREAMBLE + PARTIAL_COMPACT_UP_TO_PROMPT
        if direction == "up_to"
        else NO_TOOLS_PREAMBLE + PARTIAL_COMPACT_PROMPT
    )

    recap_messages = _build_messages_for_model(messages_to_summarize)
    if not recap_messages:
        return {"status": "no_turn"}

    # Add the prompt as the final user message
    from openjiuwen.core.foundation.llm.schema.message import UserMessage
    recap_messages.append(UserMessage(content=prompt))

    try:
        result = await adapter._model.invoke(recap_messages, temperature=0)
        raw = getattr(result, "content", None) or str(result)
    except Exception:
        logger.exception("[compact_partial] model call failed")
        return {"status": "failed", "error": "Model call failed"}

    summary = raw if isinstance(raw, str) else str(raw)
    if not summary.strip():
        return {"status": "failed", "error": "Model returned empty response"}

    # Strip <analysis> block to get clean summary
    cleaned = re.sub(r"<analysis>.*?</analysis>", "", summary, flags=re.DOTALL).strip()

    return {
        "status": "ok",
        "summary": cleaned or summary.strip(),
        "summarized_count": summarized_count,
    }
