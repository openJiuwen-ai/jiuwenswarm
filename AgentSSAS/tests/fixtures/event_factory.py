# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 虚拟 raw_event 生成器。

产生符合三层结构(common / payload / metadata)的模拟事件信号,
用于单元测试和端到端验证,不依赖 jiuwenswarm 运行时。

提供:
- create_raw_event: 灵活构造单条 raw_event
- generate_event_sequence: 产生完整的事件流(invoke_start -> llm_input ->
  tool_input -> tool_output -> llm_output -> invoke_end)
- generate_permission_interrupt_event: 产生安全检测事件
"""

from __future__ import annotations

import time
from typing import Any


def create_raw_event(
    event_type: str,
    event_class: str = "lifecycle",
    session_id: str = "test-session",
    agent_id: str = "test-agent",
    trace_id: str = "test-trace",
    interaction_seq: int = 0,
    llm_call_seq: int = -1,
    tool_call_seq: int = -1,
    tool_call_id: str = "",
    *,
    source: str = "AgentSSASSecurityRail",
    timestamp: float | None = None,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    context_id: str = "test-ctx",
    subsession_id: str = "",
) -> dict[str, Any]:
    """构造符合三层结构的 raw_event。

    common 层包含事件关联字段,payload 层包含事件内容,
    metadata 层预留扩展。缺失字段填充默认值。

    Args:
        event_type: 事件类型,如 "tool_input"。
        event_class: 事件大类,"lifecycle" 或 "security"。
        session_id: 会话 ID。
        agent_id: 智能体 ID。
        trace_id: 链路追踪 ID。
        interaction_seq: 交互序号。
        llm_call_seq: LLM 调用序号,默认 -1。
        tool_call_seq: 工具调用序号,默认 -1。
        tool_call_id: 工具调用唯一标识。
        source: 上报源标识,默认 "AgentSSASSecurityRail"。
        timestamp: 事件时间戳,None 时使用当前时间。
        payload: payload 层内容,None 时按 event_type 推导默认内容。
        metadata: metadata 层内容,None 时为空 dict。
        conversation_id: 会话展示 ID,None 时与 session_id 一致。
        context_id: 上下文 ID。
        subsession_id: 子会话 ID。

    Returns:
        符合三层结构的 raw_event dict。
    """
    if timestamp is None:
        timestamp = time.time()
    if conversation_id is None:
        conversation_id = session_id
    if metadata is None:
        metadata = {}
    if payload is None:
        payload = _default_payload(event_type)

    return {
        "common": {
            "source": source,
            "event_type": event_type,
            "event_class": event_class,
            "timestamp": timestamp,
            "interaction_seq": interaction_seq,
            "session_id": session_id,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "trace_id": trace_id,
            "context_id": context_id,
            "llm_call_seq": llm_call_seq,
            "tool_call_seq": tool_call_seq,
            "subsession_id": subsession_id,
            "tool_call_id": tool_call_id,
        },
        "payload": payload,
        "metadata": metadata,
    }


def _default_payload(event_type: str) -> dict[str, Any]:
    """按 event_type 推导默认 payload。

    生命周期事件填充典型内容,安全检测事件填充风险字段,
    未知 event_type 返回空 dict。

    Args:
        event_type: 事件类型。

    Returns:
        payload dict。
    """
    if event_type == "invoke_start":
        return {
            "content": {"query": "帮我列出当前目录下的文件"},
        }
    if event_type == "invoke_end":
        return {
            "content": {"result": "文件列表: a.txt b.txt"},
        }
    if event_type == "llm_input":
        return {
            "content": {
                "messages": [
                    {"role": "user", "content": "帮我列出当前目录下的文件"}
                ]
            },
        }
    if event_type == "llm_output":
        return {
            "content": {
                "response": "我将调用 bash 工具列出文件"
            },
        }
    if event_type == "tool_input":
        return {
            "tool_name": "bash",
            "tool_call_id": "call-001",
            "content": {"tool_args": {"command": "ls"}},
        }
    if event_type == "tool_output":
        return {
            "tool_name": "bash",
            "tool_call_id": "call-001",
            "content": {"tool_result": "a.txt\nb.txt"},
        }
    if event_type == "permission_interrupt_tool":
        return {
            "tool_name": "bash",
            "tool_call_id": "call-001",
            "content": {"tool_args": {"command": "rm -rf /"}},
            "risk_source": "PermissionInterruptRail",
            "risk_type": "tool_permission_denied",
            "risk_level": "high",
            "decision": "reject",
            "evidence": {"reason": "工具未被授权调用"},
        }
    return {}


def generate_event_sequence(
    session_id: str = "test-session",
    agent_id: str = "test-agent",
    trace_id: str = "test-trace",
    interaction_seq: int = 0,
    base_timestamp: float | None = None,
) -> list[dict[str, Any]]:
    """产生完整的事件流。

    依次生成 invoke_start -> llm_input -> tool_input -> tool_output ->
    llm_output -> invoke_end 六个事件,覆盖一次完整的工具调用交互。
    时间戳按事件顺序递增,保证顺序可识别。

    Args:
        session_id: 会话 ID。
        agent_id: 智能体 ID。
        trace_id: 链路追踪 ID。
        interaction_seq: 交互序号。
        base_timestamp: 起始时间戳,None 时使用当前时间。

    Returns:
        raw_event dict 列表,按事件流顺序排列。
    """
    if base_timestamp is None:
        base_timestamp = time.time()

    # 每个事件的时间戳递增 1 秒,保证顺序
    return [
        create_raw_event(
            "invoke_start",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=interaction_seq,
            timestamp=base_timestamp,
        ),
        create_raw_event(
            "llm_input",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=interaction_seq,
            llm_call_seq=0,
            timestamp=base_timestamp + 1,
        ),
        create_raw_event(
            "tool_input",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=interaction_seq,
            llm_call_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
            timestamp=base_timestamp + 2,
        ),
        create_raw_event(
            "tool_output",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=interaction_seq,
            llm_call_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
            timestamp=base_timestamp + 3,
        ),
        create_raw_event(
            "llm_output",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=interaction_seq,
            llm_call_seq=0,
            timestamp=base_timestamp + 4,
        ),
        create_raw_event(
            "invoke_end",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=interaction_seq,
            timestamp=base_timestamp + 5,
        ),
    ]


def generate_permission_interrupt_event(
    session_id: str = "test-session",
    agent_id: str = "test-agent",
    trace_id: str = "test-trace",
    interaction_seq: int = 0,
    tool_call_seq: int = 0,
    tool_call_id: str = "call-001",
    risk_level: str = "high",
    risk_type: str = "tool_permission_denied",
    risk_source: str = "PermissionInterruptRail",
    decision: str = "reject",
    evidence: dict[str, Any] | None = None,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """产生安全检测事件(permission_interrupt_tool)。

    event_class 为 "security",携带 risk_source、risk_type、risk_level、
    decision、evidence 等安全检测结果字段,用于验证安全护栏检测模块。

    Args:
        session_id: 会话 ID。
        agent_id: 智能体 ID。
        trace_id: 链路追踪 ID。
        interaction_seq: 交互序号。
        tool_call_seq: 工具调用序号。
        tool_call_id: 工具调用唯一标识。
        risk_level: 风险等级,默认 "high"。
        risk_type: 风险类型,默认 "tool_permission_denied"。
        risk_source: 风险来源 Rail 名。
        decision: 安全决策,默认 "reject"。
        evidence: 证据字典,None 时使用默认值。
        timestamp: 事件时间戳,None 时使用当前时间。

    Returns:
        安全检测事件 raw_event dict。
    """
    if evidence is None:
        evidence = {"reason": "工具未被授权调用"}
    if timestamp is None:
        timestamp = time.time()

    return create_raw_event(
        "permission_interrupt_tool",
        event_class="security",
        session_id=session_id,
        agent_id=agent_id,
        trace_id=trace_id,
        interaction_seq=interaction_seq,
        tool_call_seq=tool_call_seq,
        tool_call_id=tool_call_id,
        timestamp=timestamp,
        payload={
            "tool_name": "bash",
            "tool_call_id": tool_call_id,
            "content": {"tool_args": {"command": "rm -rf /"}},
            "risk_source": risk_source,
            "risk_type": risk_type,
            "risk_level": risk_level,
            "decision": decision,
            "evidence": evidence,
        },
    )
