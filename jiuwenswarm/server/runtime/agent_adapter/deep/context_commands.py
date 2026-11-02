"""上下文压缩、usage/recap/btw/repair 与部分 compact。"""

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

async def get_context_usage(adapter, session_id: str) -> dict[str, Any]:
    """获取当前上下文窗口占用统计。

    Args:
        session_id: 会话ID

    Returns:
        包含上下文使用情况统计的字典:
        - context_window_limit: 模型上下文窗口总 token 数
        - total_tokens: 当前上下文已用 token 数
        - system_prompt_tokens: 系统提示词 token 数
        - messages_tokens: 对话消息 token 数
        - tools_tokens: 工具定义 token 数
        - occupancy_rate: 占用率 (0-100)
        - message_count: 对话消息数量
        - context_occupancy: 上下文占用详情（来自 deepagent）
    """
    if not adapter._is_session_scoped_adapter:
        session_adapter = await adapter._get_or_create_session_adapter(session_id)
        try:
            return await session_adapter.get_context_usage(session_id=session_id)
        finally:
            await adapter._evict_idle_session_adapters()

    if adapter._instance is None:
        raise ValueError("Agent instance not available")

    context_engine = adapter._instance.react_agent.context_engine
    react_agent = adapter._instance.react_agent
    context = context_engine.get_context(session_id=session_id)
    if context is None:
        return {
            "context_window_limit": 0, "total_tokens": 0,
            "system_prompt_tokens": 0, "messages_tokens": 0,
            "tools_tokens": 0, "occupancy_rate": 0,
            "message_count": 0, "context_occupancy": None,
        }

    # 分项估算：直接用 context engine 的 token counter
    token_counter = context.token_counter()
    from openjiuwen.core.foundation.tool import ToolInfo

    # 系统提示词
    system_prompt = adapter._get_agent_system_prompt()
    if system_prompt and token_counter:
        system_prompt_tokens = token_counter.count(system_prompt) or 0
    elif system_prompt:
        system_prompt_tokens = len(system_prompt) // 4
    else:
        system_prompt_tokens = 0

    # 对话消息
    context_messages = context.get_messages() or []
    if context_messages and token_counter:
        messages_tokens = token_counter.count_messages(context_messages) or 0
    elif context_messages:
        messages_tokens = sum(len(str(msg.content)) // 4 for msg in context_messages)
    else:
        messages_tokens = 0

    # 工具定义
    tools: list[ToolInfo] = []
    if hasattr(react_agent, "ability_manager") and react_agent.ability_manager is not None:
        for card in react_agent.ability_manager.list() or []:
            if hasattr(card, "to_tool_info"):
                tools.append(card.to_tool_info())
            elif hasattr(card, "name") and hasattr(card, "description"):
                tools.append(ToolInfo(
                    name=card.name,
                    description=card.description or "",
                    parameters=getattr(card, "input_params", {}),
                ))
    if tools and token_counter:
        tools_tokens = token_counter.count_tools(tools) or 0
    else:
        tools_tokens = 0

    # 总量 & 窗口限制：优先用 DeepAgent 的准确值，回退到估算
    total_tokens = system_prompt_tokens + messages_tokens + tools_tokens
    context_window_limit = 0
    occupancy_rate = 0.0
    context_occupancy = None

    try:
        usage = adapter._instance.get_context_usage(session_id=session_id)
        context_occupancy = usage
        # DeepAgent 的 total_tokens 来自 usage_metadata，比估算更准确
        da_total = usage.get("total_tokens", 0)
        if da_total > 0:
            total_tokens = da_total
        context_window_limit = usage.get("context_window_tokens", 0)
        occupancy_rate = usage.get("usage_percent", 0)
    except Exception as exc:
        logger.debug("[JiuWenSwarmDeepAdapter] DeepAgent.get_context_usage failed: %s", exc)
        from openjiuwen.core.context_engine.context.context_utils import ContextUtils
        model_name = (
            getattr(adapter._model_request_config, "model_name", "") or ""
            if adapter._model_request_config else ""
        )
        context_window_limit = ContextUtils.resolve_context_max(model_name=model_name)
        if context_window_limit > 0:
            occupancy_rate = round(total_tokens / context_window_limit * 100, 1)

    message_count = len(context_messages)

    return {
        "context_window_limit": context_window_limit,
        "total_tokens": total_tokens,
        "system_prompt_tokens": system_prompt_tokens,
        "messages_tokens": messages_tokens,
        "tools_tokens": tools_tokens,
        "occupancy_rate": occupancy_rate,
        "message_count": message_count,
        "context_occupancy": context_occupancy,
    }


def _get_recent_messages(adapter, session_id: str, window: int = 30) -> list[Any]:
    """从当前 agent 对话上下文中提取最近N条消息。

    查找顺序：
    1. 当前 adapter 的 context_engine（session-scoped adapter 自身或已加载的 parent）
    2. 父 adapter 查找 session-scoped child adapter 的 context_engine
       （解决 /btw 等侧查询在 parent adapter 上执行时，会话上下文在 child adapter
       中而 parent 的 context_engine 未加载该 session 的问题）
    3. 回退到从磁盘读取兼容格式的 history 文件
    """
    # --- 快速路径：当前 adapter 的 context_engine 已加载 ---
    if adapter._instance is not None and adapter._instance.react_agent is not None:
        context_engine = adapter._instance.react_agent.context_engine
        context = context_engine.get_context(session_id=session_id)
        if context is not None:
            try:
                all_messages = list(context.get_messages() or [])
                if all_messages:
                    return all_messages[-window:]
            except Exception as exc:
                logger.debug("[JiuWenSwarmDeepAdapter] _get_recent_messages from context_engine failed: %s", exc)

    # --- 中间路径：从 session-scoped child adapter 的 context_engine 查找 ---
    # 当 /btw 等侧查询在 parent adapter 上执行时，会话上下文实际在
    # session-scoped child adapter 的内存中（context_engine），而非磁盘。
    # 直接从内存读取可避免与异步写队列的"写后读"竞态。
    if not getattr(adapter, "_is_session_scoped_adapter", False):
        session_adapter = adapter._get_cached_session_adapter(session_id)
        if session_adapter is not None:
            inst = getattr(session_adapter, "_instance", None)
            if inst is not None and getattr(inst, "react_agent", None) is not None:
                ctx_eng = inst.react_agent.context_engine
                ctx = ctx_eng.get_context(session_id=session_id)
                if ctx is not None:
                    try:
                        all_msgs = list(ctx.get_messages() or [])
                        if all_msgs:
                            logger.debug(
                                "[JiuWenSwarmDeepAdapter] _get_recent_messages: "
                                "read %d messages from session-scoped adapter context_engine "
                                "for session %s",
                                len(all_msgs),
                                session_id,
                            )
                            return all_msgs[-window:]
                    except Exception as exc:
                        logger.debug(
                            "[JiuWenSwarmDeepAdapter] _get_recent_messages "
                            "from session-scoped adapter context_engine failed: %s",
                            exc,
                        )

    # --- 回退路径：从磁盘读取兼容格式 history ---
    # 典型场景：/resume 之后，context_engine 还未加载新 session 的上下文，
    # 但磁盘上已有该 session 的历史消息。
    try:
        from types import SimpleNamespace

        from jiuwenswarm.server.runtime.session.session_history import (
            load_history_records,
        )
        records = load_history_records(session_id)
        if not records:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] _get_recent_messages: no history records on disk for session %s",
                session_id,
            )
            return []

        # 过滤出适合 recap 的消息记录
        # 只保留 user 消息和 assistant 的最终回复 / compact summary
        recapworthy = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            role = rec.get("role")
            content = rec.get("content")
            if not isinstance(content, str) or not content.strip():
                continue

            if role == "user":
                recapworthy.append(SimpleNamespace(role="user", content=content))
            elif role == "assistant":
                # Goal-completed cards are UI-only history facts; never feed them
                # back into model recap context.
                if rec.get("is_goal_completed_message"):
                    continue
                event_type = rec.get("event_type")
                # 只包含 assistant 的最终回复和 compact summary
                if event_type in ("chat.final", "context.compact_summary") or not event_type:
                    recapworthy.append(SimpleNamespace(role="assistant", content=content))

        if not recapworthy:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] _get_recent_messages: no recap-worthy records on disk for session %s",
                session_id,
            )
            return []

        logger.info(
            "[JiuWenSwarmDeepAdapter] _get_recent_messages: loaded %d records from disk for session %s "
            "(context_engine fallback)",
            len(recapworthy),
            session_id,
        )
        return recapworthy[-window:]
    except Exception as exc:
        logger.debug("[JiuWenSwarmDeepAdapter] _get_recent_messages disk fallback failed: %s", exc)
        return []


async def _get_agent_tools(adapter, session_id: str) -> list[Any]:
    """取主 agent 当前 tools 列表（List[ToolInfo]），用于 btw/recap 透传给模型。

    透传 tools schema 是为了与主 agent 保持 cache key 一致（openjiuwen 的
    prompt cache 布局为 tools → system → messages，tools 段缺失会破坏前缀
    匹配）。工具执行仍被禁用：btw/recap 单轮 + tool_use 检测丢弃。

    查找顺序与 _get_recent_messages 一致：先当前 adapter 的 react_agent，
    再 session-scoped child adapter（btw 在 parent 执行时 tools 在 child）。
    返回空列表表示无工具可用，调用方应按不传 tools 处理（tools or None）。
    """
    async def _from(inst: Any) -> list[Any]:
        ra = getattr(inst, "react_agent", None)
        if ra is None:
            return []
        am = getattr(ra, "ability_manager", None)
        if am is None or not callable(getattr(am, "list_tool_info", None)):
            return []
        try:
            return list(await am.list_tool_info() or [])
        except Exception as exc:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] _get_agent_tools list_tool_info failed: %s",
                exc,
            )
            return []

    # 1) 当前 adapter
    if adapter._instance is not None:
        tools = await _from(adapter._instance)
        if tools:
            return tools

    # 2) session-scoped child adapter（btw 等侧查询在 parent 执行时 tools 在 child）
    if not getattr(adapter, "_is_session_scoped_adapter", False):
        session_adapter = adapter._get_cached_session_adapter(session_id)
        if session_adapter is not None:
            inst = getattr(session_adapter, "_instance", None)
            if inst is not None:
                return await _from(inst)

    return []


def _get_agent_system_prompt(adapter) -> str:
    """Return the current agent's system prompt, or empty string if unavailable.

    Result is cached since the system prompt is derived from project context
    (CLAUDE.md, skills, etc.) which doesn't change within a session.
    Reusing the same bytes is critical for prompt cache prefix matching.
    """
    if adapter._last_system_prompt:
        return adapter._last_system_prompt
    if adapter._instance is None or adapter._instance.react_agent is None:
        return ""
    react_agent = adapter._instance.react_agent
    if hasattr(react_agent, "prompt_builder") and react_agent.prompt_builder is not None:
        adapter._last_system_prompt = react_agent.prompt_builder.build()
        return adapter._last_system_prompt
    if hasattr(react_agent, "system_prompt_builder") and react_agent.system_prompt_builder is not None:
        adapter._last_system_prompt = react_agent.system_prompt_builder.build()
        return adapter._last_system_prompt
    return ""


async def generate_btw_answer(adapter, session_id: str, question: str) -> dict[str, Any]:
    """回答 /btw 侧问题：独立、无工具、单轮 LLM 查询。

    prompt cache 策略：
    - 共享主 agent 的 system prompt（项目上下文、skills、CLAUDE.md 等）
    - 保持消息原始格式（含 structured content blocks）以实现 byte-identical 前缀
    - 最后一条 pre-prompt 消息添加 cache_control marker（ephemeral）
    - btw prompt 不添加 cache_control（skipCacheWrite）
    - 透传主 agent tools schema 保 cache key，但单轮 + tool_use 丢弃 = 禁止执行
    - 不修改对话历史（read-only）

    Args:
        session_id: 会话ID
        question: 用户侧问题

    Returns:
        {"status": "ok", "answer": "..."} 或 {"status": "no_context"|"failed", ...}
    """
    from jiuwenswarm.server.runtime.agent_adapter.recap_prompts import (
        RECENT_MESSAGE_WINDOW,
        _build_btw_prompt,
    )

    # 1) 获取 system prompt（与主 agent 相同，已缓存）
    system_prompt = adapter._get_agent_system_prompt()

    # 2) 获取最近对话消息（保持原始格式，不做 str() 转换）
    messages = adapter._get_recent_messages(session_id, window=RECENT_MESSAGE_WINDOW)
    if not messages and not system_prompt:
        return {"status": "no_context"}

    # 2.5) 取主 agent tools（透传给模型以保 cache key；工具执行由单轮 +
    #      tool_use 检测丢弃禁止，对齐 claude-code canUseTool:deny）
    tools = await adapter._get_agent_tools(session_id)

    # 3) 构建 btw prompt（system prompt 通过 SystemMessage 传递，不嵌入文本）
    prompt = _build_btw_prompt(
        question=question,
        language=adapter._resolve_prompt_language(),
    )

    # 4) 调用模型 — system_prompt 作为 SystemMessage 前置，prompt 作为 UserMessage
    # enable_prompt_caching=True 启用 cache_control marker
    answer = await adapter._call_model_for_recap(
        messages, prompt, system_prompt=system_prompt, enable_prompt_caching=True,
        tools=tools or None,
    )
    if not answer:
        return {"status": "failed", "error": "Model returned empty response"}

    return {"status": "ok", "answer": answer.strip()}


async def repair_model_response(adapter, prompt: str) -> str | None:
    """Run a focused repair prompt using the currently selected chat model."""
    if adapter._model is None:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] repair skipped: no model instance available "
            "(cache_keys=%s default=%s)",
            list(adapter._model_cache)[:5], adapter._default_model_name or "<unset>",
        )
        return None
    from openjiuwen.core.foundation.llm.schema.message import UserMessage

    result = await adapter._model.invoke(
        [UserMessage(content=prompt)],
        temperature=0,
    )
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    output = getattr(result, "output", None)
    if isinstance(output, str):
        return output
    return str(result) if result is not None else None


async def _count_full_context_tokens(
    adapter,
    context: Any,
    react_agent: Any,
    session_id: str,
) -> int:
    """计算完整上下文的 token 数（包含 system messages + context messages + tools）。
    Args:
        context: ModelContext 对象
        react_agent: ReActAgent 对象
        session_id: 会话ID

    Returns:
        完整上下文的 token 总数
    """
    from openjiuwen.core.foundation.tool import ToolInfo

    token_counter = context.token_counter()
    total_tokens = 0

    # 1. 计算系统消息的 tokens
    system_prompt = adapter._get_agent_system_prompt()

    if system_prompt:
        if token_counter is not None:
            total_tokens += token_counter.count(system_prompt)
        else:
            total_tokens += len(system_prompt) // 4

    # 2. 计算对话消息的 tokens
    context_messages = context.get_messages()
    if context_messages:
        if token_counter is not None:
            total_tokens += token_counter.count_messages(context_messages)
        else:
            total_tokens += sum(len(str(msg.content)) // 4 for msg in context_messages)

    # 3. 计算工具定义的 tokens
    tools: list[ToolInfo] = []
    if hasattr(react_agent, "ability_manager") and react_agent.ability_manager is not None:
        for card in react_agent.ability_manager.list() or []:
            if hasattr(card, "to_tool_info"):
                tools.append(card.to_tool_info())
            elif hasattr(card, "name") and hasattr(card, "description"):
                tools.append(ToolInfo(
                    name=card.name,
                    description=card.description or "",
                    parameters=getattr(card, "input_params", {}),
                ))

    if tools and token_counter is not None:
        total_tokens += token_counter.count_tools(tools)

    return total_tokens
