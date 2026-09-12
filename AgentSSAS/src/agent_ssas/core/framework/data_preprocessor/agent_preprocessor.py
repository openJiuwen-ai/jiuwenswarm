# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 格式解析器。

将 AgentSSASSecurityRail 上报的三层结构 raw_event(原始事件 dict)
解析为引擎内部统一的 UnifiedEvent。

按文档 5.9 节实现解析逻辑,按文档 5.5-5.7 节实现 node_id 生成、
parent_node_id/next_node_id 规则和字段保留与映射。
按文档 5.10 节处理无 session_start/end 事件时的自动 node 生成。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.event import (
    EventNode,
    Interaction,
    Session,
    Trace,
    UnifiedEvent,
)
from agent_ssas.core.framework.utils.id_utils import new_event_id

logger = logging.getLogger(__name__)

# LRU 缓池最大 session 数, 防止长期运行内存泄漏
_MAX_CACHED_SESSIONS = 100

# event_type → node_type 的映射表(文档 5.7 节、5.8.2 节)
_EVENT_TYPE_TO_NODE_TYPE: dict[str, str] = {
    "invoke_start": "interaction",
    "invoke_end": "interaction",
    "llm_input": "llm_call",
    "llm_output": "llm_call",
    "tool_input": "tool_call",
    "tool_output": "tool_call",
    "permission_interrupt_tool": "tool_call",
}

# event_type → action_name 的固定映射(文档 5.8.2 节内容字段映射清单)
_FIXED_ACTION_NAME: dict[str, str] = {
    "invoke_start": "invoke_start",
    "invoke_end": "invoke_end",
    "llm_input": "llm_call",
    "llm_output": "llm_call",
}

# payload content 子字段名(按 event_type 映射)
# input 端取 content 中的哪个字段作为 input_content
_INPUT_CONTENT_FIELD: dict[str, str] = {
    "invoke_start": "query",
    "llm_input": "messages",
    "tool_input": "tool_args",
    "permission_interrupt_tool": "tool_args",
}

# output 端取 content 中的哪个字段作为 output_content
_OUTPUT_CONTENT_FIELD: dict[str, str] = {
    "invoke_end": "result",
    "llm_output": "response",
    "tool_output": "tool_result",
}

# 触发聚合的结束事件类型
_END_EVENT_TYPES = {"tool_output", "llm_output", "invoke_end"}


def _safe_int(value: Any, default: int = -1) -> int:
    """安全转换为 int,转换失败时返回默认值。

    参数:
        value: 原始值。
        default: 转换失败时的默认值。

    返回: 转换后的 int 值。
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any) -> str:
    """安全转换为 str,None 转为空字符串。

    参数 value: 原始值。

    返回: 转换后的 str 值。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _content_to_str(content: Any) -> str:
    """将 content 字段转换为字符串表示。

    str 类型直接返回;dict/list 等结构序列化为 JSON 字符串;
    None 返回空字符串。便于检测模块以统一视角处理内容字段。

    参数 content: 原始 content 值。

    返回: 字符串形式的内容。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


class AgentSSASPreprocessor:
    """AgentSSAS 格式解析器。

    将 AgentSSASSecurityRail 上报的三层结构 raw_event 解析为 UnifiedEvent。

    解析流程(文档 5.9 节):
    1. 从 common 层提取 source、event_type、event_class、timestamp、各 ID 字段
       (含 tool_call_id)
    2. 根据 event_type 和 event_class 确定 node_type,生成 node_id
    3. 从 payload 层提取 content、tool_name、exception
    4. 提取安全检测结果字段(risk_source、risk_type、risk_level),
       event_class == "security" 时标记 is_risk_event = True
    5. 生成 event_id(UUID)
    6. 维护 Trace、Session、Interaction 的增量构建
    7. 处理无 session_start/end 事件时的自动 node 生成(5.10 节)
    8. 设置 parent_node_id 和 next_node_id
    9. 缺失字段填充默认值
    """

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化解析器.

        参数 config: SSAS 配置实例.
        """
        self._config = config
        # 并发安全锁: report_event 是 async, 可能并发调用 parse
        self._lock = asyncio.Lock()
        # trace_id → Trace 的缓存(增量构建)
        self._traces: dict[str, Trace] = {}
        # session_id → Session 的缓存(增量构建)
        # 用 OrderedDict 实现 LRU: 最近访问的在末尾, 满了从头部淘汰
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        # (session_id, interaction_seq) → Interaction 的缓存(增量构建)
        self._interactions: dict[tuple[str, int], Interaction] = {}
        # 每个 session 上一个非 session_start/end 节点的 node_id(用于设置 next_node_id)
        self._last_node_id: dict[str, str] = {}

    async def parse(self, raw_event: dict) -> list[UnifiedEvent]:
        """解析 raw_event 为 UnifiedEvent 列表。

        返回事件列表:[自动生成的派生事件(如有)] + [当前事件]。
        当首次遇到某 session 时自动生成 session_start 事件(文档 5.10 节),
        当首次遇到某 interaction 且当前事件不是 invoke_start 时自动生成
        user_input 事件。这些派生事件排在当前事件之前返回。

        参数 raw_event: 原始事件 dict(包含 common / payload / metadata 三层)。

        返回: UnifiedEvent 列表,首元素可能是派生事件,末尾是当前事件。

        异常:
            ValueError: raw_event 不是 dict 或缺少 common 层时。
        """
        if not isinstance(raw_event, dict):
            raise ValueError(f"raw_event 必须为 dict,实际类型: {type(raw_event).__name__}")

        common = raw_event.get("common")
        if not isinstance(common, dict):
            raise ValueError("raw_event 缺少 common 层或 common 不是 dict")

        async with self._lock:
            return await self._parse_locked(raw_event, common)

    async def _parse_locked(self, raw_event: dict, common: dict) -> list[UnifiedEvent]:
        """在锁内执行实际解析逻辑.

        参数:
            raw_event: 原始事件 dict.
            common: 已校验的 common 层 dict.

        返回: UnifiedEvent 列表.
        """

        payload = raw_event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        # 提取 common 层字段
        source = _safe_str(common.get("source"))
        event_type = _safe_str(common.get("event_type"))
        event_class = _safe_str(common.get("event_class"))
        timestamp = float(common.get("timestamp", 0.0) or 0.0)
        session_id = _safe_str(common.get("session_id"))
        agent_id = _safe_str(common.get("agent_id"))
        trace_id = _safe_str(common.get("trace_id"))
        interaction_seq = _safe_int(common.get("interaction_seq"), -1)
        llm_call_seq = _safe_int(common.get("llm_call_seq"), -1)
        tool_call_seq = _safe_int(common.get("tool_call_seq"), -1)
        tool_call_id = _safe_str(common.get("tool_call_id"))

        # 确定 node_type
        node_type = self._determine_node_type(event_type, event_class)

        # 增量构建 Trace / Session / Interaction,并处理自动 node 生成(5.10 节)
        # auto_events 收集自动生成的派生事件(session_start, user_input)
        trace, session, interaction, auto_events = self._ensure_structures(
            trace_id=trace_id,
            session_id=session_id,
            interaction_seq=interaction_seq,
            agent_id=agent_id,
            source=source,
            timestamp=timestamp,
            event_type=event_type,
        )

        # 生成 node_id
        node_id = self._generate_node_id(
            node_type=node_type,
            session_id=session_id,
            interaction_seq=interaction_seq,
            tool_call_seq=tool_call_seq,
            llm_call_seq=llm_call_seq,
            tool_call_id=tool_call_id,
        )

        # 提取 payload 层字段
        tool_name = _safe_str(payload.get("tool_name"))
        content = payload.get("content", {})
        if not isinstance(content, dict):
            content = {}
        exception = payload.get("exception")

        # 确定内容字段
        input_content, output_content, action_name = self._extract_content_fields(
            event_type=event_type,
            content=content,
            tool_name=tool_name,
            exception=exception,
            payload=payload,
        )

        # 提取安全检测结果字段
        risk_source = _safe_str(payload.get("risk_source"))
        risk_type = _safe_str(payload.get("risk_type"))
        risk_level = _safe_str(payload.get("risk_level"))
        is_risk_event = event_class == "security"

        # 构建 risk_assessment(文档 5.7 节:decision 和 evidence 存入 risk_assessment)
        risk_assessment: dict | None = None
        if is_risk_event:
            risk_assessment = {}
            decision = payload.get("decision")
            evidence = payload.get("evidence")
            if decision is not None:
                risk_assessment["decision"] = decision
            if evidence is not None:
                risk_assessment["evidence"] = evidence

        # 设置 parent_node_id 和 next_node_id
        parent_node_id = self._determine_parent_node_id(
            node_type=node_type,
            session_id=session_id,
            session=session,
            interaction_seq=interaction_seq,
            interaction=interaction,
        )
        next_node_id = self._determine_next_node_id(
            node_type=node_type,
            event_type=event_type,
            session_id=session_id,
            session=session,
            interaction_seq=interaction_seq,
            interaction=interaction,
        )

        # 构建 EventNode
        event_node = EventNode(
            node_id=node_id,
            node_type=node_type,
            parent_node_id=parent_node_id,
            next_node_id=next_node_id,
            session_id=session_id,
            interaction_seq=interaction_seq,
            agent_id=agent_id,
            input_content=input_content,
            output_content=output_content,
            action_name=action_name,
            event_type=event_type,
            event_class=event_class,
            source=source,
            timestamp=timestamp,
            llm_call_seq=llm_call_seq,
            tool_call_seq=tool_call_seq,
            tool_call_id=tool_call_id,
            is_risk_event=is_risk_event,
            risk_source=risk_source,
            risk_type=risk_type,
            risk_level=risk_level,
            risk_assessment=risk_assessment,
        )

        # 将节点注册到 Trace 的全局索引
        trace.all_nodes[node_id] = event_node

        # 更新 Interaction 和 Session 结构
        self._update_interaction(
            interaction=interaction,
            node_type=node_type,
            event_type=event_type,
            node_id=node_id,
        )
        self._update_session(
            session=session,
            node_type=node_type,
            event_type=event_type,
            node_id=node_id,
        )

        # 更新上一个节点(用于跨节点 next_node_id 链接)
        if node_type != "session":
            self._last_node_id[session_id] = node_id

        # 生成 event_id
        event_id = new_event_id()

        main_event = UnifiedEvent(
            event_node=event_node,
            trace=trace,
            event_id=event_id,
        )

        # 返回派生事件 + 当前事件
        return auto_events + [main_event]

    def _determine_node_type(self, event_type: str, event_class: str) -> str:
        """根据 event_type 和 event_class 确定 node_type。

        参数:
            event_type: 事件类型。
            event_class: 事件大类。

        返回: 节点类型字符串。

        异常:
            ValueError: 无法识别的 event_type 时。
        """
        node_type = _EVENT_TYPE_TO_NODE_TYPE.get(event_type)
        if node_type is not None:
            return node_type
        raise ValueError(f"无法识别的 event_type: {event_type!r}")

    def _ensure_structures(
        self,
        trace_id: str,
        session_id: str,
        interaction_seq: int,
        agent_id: str,
        source: str,
        timestamp: float,
        event_type: str,
    ) -> tuple[Trace, Session, Interaction | None, list[UnifiedEvent]]:
        """确保 Trace、Session、Interaction 结构存在,处理自动 node 生成。

        按文档 5.10 节:
        - 收到第一个事件时自动生成 session_start 节点
        - 收到第一个某 interaction 的事件时自动生成 user_input 节点

        自动生成的节点同时封装为 UnifiedEvent 返回,供接入适配模块写入 events 表。

        参数:
            trace_id: 追踪 ID。
            session_id: 会话 ID。
            interaction_seq: 交互序号。
            agent_id: 智能体 ID。
            source: 上报源。
            timestamp: 事件时间戳。
            event_type: 事件类型。

        返回: (Trace, Session, Interaction 或 None, auto_events) 元组。
            auto_events 是自动生成的派生事件列表。
        """
        auto_events: list[UnifiedEvent] = []

        # 确保 Trace 存在
        trace = self._traces.get(trace_id)
        if trace is None:
            trace = Trace(
                trace_id=trace_id,
                source_info={},
                sessions=[],
                all_nodes={},
                all_data={},
            )
            self._traces[trace_id] = trace

        # 确保 Session 存在(5.10 节:自动生成 session)
        session = self._sessions.get(session_id)
        if session is not None:
            # 已存在: 移到末尾(LRU 最近使用)
            self._sessions.move_to_end(session_id)
        else:
            # 新 session: 检查 LRU 上限, 满则淘汰最久未使用的
            if len(self._sessions) >= _MAX_CACHED_SESSIONS:
                evicted_sid, _evicted_session = self._sessions.popitem(last=False)
                self._evict_session_cache(evicted_sid)
                logger.info("LRU 淘汰 session 缓存: %s", evicted_sid)
            session = Session(
                session_id=session_id,
                session_node_id="",
                interactions=[],
            )
            self._sessions[session_id] = session
            trace.sessions.append(session)

            # 自动生成 session 节点(5.10 节)
            auto_event = self._create_auto_session(
                trace=trace,
                session_id=session_id,
                agent_id=agent_id,
                source=source,
                timestamp=timestamp,
            )
            if auto_event is not None:
                auto_events.append(auto_event)

        # 确保 Interaction 存在(5.10 节:自动生成 interaction)
        interaction: Interaction | None = None
        if interaction_seq >= 0:
            interaction_key = (session_id, interaction_seq)
            interaction = self._interactions.get(interaction_key)
            if interaction is None:
                interaction = Interaction(
                    interaction_seq=interaction_seq,
                    session_id=session_id,
                    interaction_node_id="",
                    tool_call_node_ids=[],
                    llm_call_node_ids=[],
                )
                self._interactions[interaction_key] = interaction
                session.interactions.append(interaction)

                # 自动生成 interaction 节点(5.10 节),除非当前事件本身就是 invoke_start
                if event_type != "invoke_start":
                    auto_event = self._create_auto_interaction(
                        trace=trace,
                        session=session,
                        session_id=session_id,
                        interaction_seq=interaction_seq,
                        agent_id=agent_id,
                        source=source,
                        timestamp=timestamp,
                    )
                    if auto_event is not None:
                        auto_events.append(auto_event)

        return trace, session, interaction, auto_events

    def _evict_session_cache(self, session_id: str) -> None:
        """清理指定 session 的所有缓存.

        在 LRU 淘汰时调用, 清理 _interactions 和 _last_node_id 中
        属于该 session 的条目, 防止内存泄漏.

        参数 session_id: 被淘汰的会话 ID.
        """
        # 清理该 session 的所有 interaction 缓存
        keys_to_remove = [k for k in self._interactions if k[0] == session_id]
        for k in keys_to_remove:
            del self._interactions[k]

        # 清理 last_node_id
        self._last_node_id.pop(session_id, None)

    def _create_auto_session(
        self,
        trace: Trace,
        session_id: str,
        agent_id: str,
        source: str,
        timestamp: float,
    ) -> UnifiedEvent | None:
        """自动生成 session 节点(文档 5.10 节)。

        当收到第一个事件且对应 Session 尚不存在时,自动生成 session 节点,
        保证 Trace/Session 结构的完整性。

        参数:
            trace: 所属 Trace。
            session_id: 会话 ID。
            agent_id: 智能体 ID。
            source: 上报源。
            timestamp: 事件时间戳。

        返回: 封装为 UnifiedEvent 的 session_start 派生事件。
        """
        node_id = f"{session_id}_session"
        node = EventNode(
            node_id=node_id,
            node_type="session",
            parent_node_id="",
            next_node_id="",
            session_id=session_id,
            interaction_seq=-1,
            agent_id=agent_id,
            input_content="",
            output_content="",
            action_name="session_start",
            event_type="session_start",
            event_class="lifecycle",
            source=source or "AgentSSASSecurityRail",
            timestamp=timestamp or time.time(),
            llm_call_seq=-1,
            tool_call_seq=-1,
            is_risk_event=False,
        )
        trace.all_nodes[node_id] = node

        # 更新 Session 的 session_node_id
        session = self._sessions.get(session_id)
        if session is not None:
            session.session_node_id = node_id

        return UnifiedEvent(
            event_node=node,
            trace=trace,
            event_id=new_event_id(),
        )

    def _create_auto_interaction(
        self,
        trace: Trace,
        session: Session,
        session_id: str,
        interaction_seq: int,
        agent_id: str,
        source: str,
        timestamp: float,
    ) -> UnifiedEvent | None:
        """自动生成 interaction 节点(文档 5.10 节)。

        当收到第一个某 interaction 的事件且缺少显式用户输入事件时,
        自动生成 interaction 节点。

        参数:
            trace: 所属 Trace。
            session: 所属 Session。
            session_id: 会话 ID。
            interaction_seq: 交互序号。
            agent_id: 智能体 ID。
            source: 上报源。
            timestamp: 事件时间戳。

        返回: 封装为 UnifiedEvent 的 user_input 派生事件。
        """
        node_id = f"{session_id}_{interaction_seq}_interaction"
        session_node_id = session.session_node_id or ""
        node = EventNode(
            node_id=node_id,
            node_type="interaction",
            parent_node_id=session_node_id,
            next_node_id="",
            session_id=session_id,
            interaction_seq=interaction_seq,
            agent_id=agent_id,
            input_content="",
            output_content="",
            action_name="user_query",
            event_type="user_input",
            event_class="lifecycle",
            source=source or "AgentSSASSecurityRail",
            timestamp=timestamp or time.time(),
            llm_call_seq=-1,
            tool_call_seq=-1,
            is_risk_event=False,
        )
        trace.all_nodes[node_id] = node

        # 更新 Interaction 的 interaction_node_id
        interaction_key = (session_id, interaction_seq)
        interaction = self._interactions.get(interaction_key)
        if interaction is not None:
            interaction.interaction_node_id = node_id

        # 更新 session 的 next_node_id 指向第一个 interaction(文档 5.6 节)
        if session.session_node_id:
            session_node = trace.all_nodes.get(session.session_node_id)
            if session_node is not None and not session_node.next_node_id:
                session_node.next_node_id = node_id

        # 更新上一个节点(用于跨节点 next_node_id 链接)
        self._last_node_id[session_id] = node_id

        return UnifiedEvent(
            event_node=node,
            trace=trace,
            event_id=new_event_id(),
        )

    def _generate_node_id(
        self,
        node_type: str,
        session_id: str,
        interaction_seq: int,
        tool_call_seq: int,
        llm_call_seq: int,
        tool_call_id: str,
    ) -> str:
        """根据节点类型和序号生成 node_id(文档 5.5 节)。

        参数:
            node_type: 节点类型。
            session_id: 会话 ID。
            interaction_seq: 交互序号。
            tool_call_seq: 工具调用序号。
            llm_call_seq: LLM 调用序号。
            tool_call_id: 工具调用唯一标识。

        返回: node_id 字符串。
        """
        if node_type == "session":
            return f"{session_id}_session"
        if node_type == "interaction":
            return f"{session_id}_{interaction_seq}_interaction"
        if node_type == "tool_call":
            return f"{session_id}_{interaction_seq}_toolcall_{tool_call_seq}"
        if node_type == "llm_call":
            return f"{session_id}_{interaction_seq}_llmcall_{llm_call_seq}"
        # 未知类型回退:用 session_id 加 node_type
        return f"{session_id}_{node_type}"

    def _extract_content_fields(
        self,
        event_type: str,
        content: dict,
        tool_name: str,
        exception: Any,
        payload: dict | None = None,
    ) -> tuple[str, str, str]:
        """提取内容字段 input_content、output_content、action_name。

        按文档 5.8.2 节内容字段映射清单:
        - invoke_start: action_name="invoke_start", input_content=query, output_content=""
        - invoke_end: action_name="invoke_end", input_content="", output_content=result
        - llm_input: action_name="llm_call", input_content=messages, output_content=""
        - llm_output: action_name="llm_call", input_content="", output_content=response
        - tool_input: action_name=tool_name, input_content=tool_args, output_content=""
        - tool_output: action_name=tool_name, input_content="", output_content=tool_result
        - permission_interrupt_tool: action_name=tool_name, input_content=tool_args, output_content=""

        参数:
            event_type: 事件类型。
            content: payload.content 子结构。
            tool_name: 工具名称。
            exception: 异常信息。

        返回: (input_content, output_content, action_name) 元组。
        """
        action_name = ""
        input_content = ""
        output_content = ""

        # 确定 action_name
        if event_type in _FIXED_ACTION_NAME:
            action_name = _FIXED_ACTION_NAME[event_type]
        elif event_type in ("tool_input", "tool_output", "permission_interrupt_tool"):
            action_name = tool_name
        else:
            action_name = event_type

        # 确定内容字段(优先从 content 子结构提取,回退从 payload 直接获取)
        if event_type in _INPUT_CONTENT_FIELD:
            field_name = _INPUT_CONTENT_FIELD[event_type]
            input_content = _content_to_str(content.get(field_name))
            # 回退:如果 content 中没有,尝试从 payload 直接获取
            if not input_content and payload and field_name in payload:
                input_content = _content_to_str(payload.get(field_name))

        if event_type in _OUTPUT_CONTENT_FIELD:
            field_name = _OUTPUT_CONTENT_FIELD[event_type]
            output_content = _content_to_str(content.get(field_name))
            # 回退:如果 content 中没有,尝试从 payload 直接获取
            if not output_content and payload and field_name in payload:
                output_content = _content_to_str(payload.get(field_name))

        # 异常事件:exception 存入 output_content(文档 5.7 节)
        if exception is not None and not output_content:
            output_content = _content_to_str(exception)

        return input_content, output_content, action_name

    def _determine_parent_node_id(
        self,
        node_type: str,
        session_id: str,
        session: Session,
        interaction_seq: int,
        interaction: Interaction | None,
    ) -> str:
        """确定 parent_node_id(文档 5.6 节)。

        parent_node_id 规则:
        - session: 空(顶层节点)
        - interaction: session 的 node_id
        - tool_call / llm_call: interaction 的 node_id

        参数:
            node_type: 节点类型。
            session_id: 会话 ID。
            session: 所属 Session。
            interaction_seq: 交互序号。
            interaction: 所属 Interaction。

        返回: parent_node_id 字符串。
        """
        if node_type == "session":
            return ""

        # interaction 的父节点是 session
        if node_type == "interaction":
            return session.session_node_id

        # tool_call / llm_call 的父节点是 interaction
        if node_type in ("tool_call", "llm_call"):
            if interaction is not None and interaction.interaction_node_id:
                return interaction.interaction_node_id
            # 回退:自动生成的 interaction 节点 id
            return f"{session_id}_{interaction_seq}_interaction"

        return ""

    def _determine_next_node_id(
        self,
        node_type: str,
        event_type: str,
        session_id: str,
        session: Session,
        interaction_seq: int,
        interaction: Interaction | None,
    ) -> str:
        """确定 next_node_id。

        0.1 版本采用简单策略:结束事件(tool_output / llm_output / invoke_end)
        暂不设置 next_node_id(后续事件到达时由调用方补全)。
        session 的 next_node_id 由后续 session 的 session 节点补全。

        参数:
            node_type: 节点类型。
            event_type: 事件类型。
            session_id: 会话 ID。
            session: 所属 Session。
            interaction_seq: 交互序号。
            interaction: 所属 Interaction。

        返回: next_node_id 字符串(0.1 版本通常返回空字符串)。
        """
        # session 的 next_node_id 指向第一个 interaction
        if node_type == "session":
            if session.interactions:
                return session.interactions[0].interaction_node_id
            return ""

        return ""

    def _update_interaction(
        self,
        interaction: Interaction | None,
        node_type: str,
        event_type: str,
        node_id: str,
    ) -> None:
        """更新 Interaction 结构,将节点 id 注册到对应字段。

        参数:
            interaction: 所属 Interaction(可能为 None)。
            node_type: 节点类型。
            event_type: 事件类型。
            node_id: 节点 ID。
        """
        if interaction is None:
            return

        if node_type == "interaction":
            interaction.interaction_node_id = node_id
        elif node_type == "tool_call":
            interaction.tool_call_node_ids.append(node_id)
        elif node_type == "llm_call":
            # llm_input 和 llm_output 共享同一个 llm_call 节点 id,
            # 仅在第一次(llm_input)时追加,避免重复
            if node_id not in interaction.llm_call_node_ids:
                interaction.llm_call_node_ids.append(node_id)

    def _update_session(
        self,
        session: Session,
        node_type: str,
        event_type: str,
        node_id: str,
    ) -> None:
        """更新 Session 结构,将节点 id 注册到对应字段。

        参数:
            session: 所属 Session。
            node_type: 节点类型。
            event_type: 事件类型。
            node_id: 节点 ID。
        """
        if node_type == "session":
            session.session_node_id = node_id
