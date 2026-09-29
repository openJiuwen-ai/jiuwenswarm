# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""诊断 Agent 化链路（设计 v2 §2.6）：E2A envelope 组装 + 流事件映射。

分析阶段不再由 Gateway 直构 Model 单轮调用，而是走 AgentServer 完整
Agent 流程（React 循环 / 工具调用 / 上下文压缩）：Gateway 组
``chat.send`` 信封（channel=diagnosis、一次性 session、project_dir 指向
被诊断会话的代码目录），流式消费 ``AgentResponseChunk``，映射为诊断 SSE 事件。

本模块为纯函数层（envelope 组装 / chunk 映射），IO 编排在
``gateway/channel_manager/web/diagnose_http.py``。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod

# 诊断内部通道（同 cron/heartbeat 待遇：source=system、跳过 file hint/cron 工具）
DIAGNOSIS_CHANNEL_ID = "diagnosis"

# 映射到诊断 SSE 的流事件（其余 chat.* 事件静默忽略：notice/usage/process 等
# 与诊断报告渲染无关，避免 SSE 噪音）
_EVENT_TOKEN = "chat.delta"
_EVENT_TOOL = "chat.tool_update"
_EVENT_ERROR = "chat.error"
_EVENT_FINAL = "chat.final"
# DeepAgent 交互循环错误（interface_deep ERROR_EVENT_TYPE）：payload 带
# code/message（无 error 字段），真实失败原因在这里；漏映射会被静默吞掉，
# 表现为"未收到任何报告内容"的空报告
_EVENT_EXECUTION_ERROR = "execution.error"


@dataclass
class DiagnosisAgentRequest:
    """组装诊断信封所需的输入（由 diagnose_http 从证据收集阶段产出）。"""

    prompt: str
    """诊断任务全文（摘要层 + 证据包路径 + 查证指令，见 prompts.build_agent_prompt）。"""

    project_dir: str = ""
    """被诊断会话的 project_dir（Agent 代码检索的工作目录）；空则默认工作区。"""

    mode: str = "agent"
    """Agent 运行 mode（沿用被诊断会话的 mode，agent/team 各自的适配器）。"""


def make_diagnosis_session_id(*, now_ts: float | None = None) -> str:
    """一次性诊断 session id：``diagnosis_{ts_hex}_{rand}``。

    前缀 ``diagnosis`` 驱动三处隔离（channel 白名单 / oneshot 回收 /
    trace sink drop），时间戳十六进制 + 随机后缀保证不复用。
    """
    ts = format(int(now_ts if now_ts is not None else time.time() * 1000), "x")
    return f"diagnosis_{ts}_{secrets.token_hex(3)}"


def build_diagnosis_envelope(
    request: DiagnosisAgentRequest,
    *,
    session_id: str,
    now_ts: float | None = None,
):
    """组装诊断 ``chat.send`` E2A 信封（流式）。

    params 口径对齐 cron 的 chat.send（`scheduler.py` 同构）：query 为任务全文，
    mode/project_dir 决定 Agent 工作目录与适配器；不带 cron 元数据。
    """
    params: dict[str, Any] = {
        "content": request.prompt,
        "query": request.prompt,
        "mode": request.mode,
        # run.kind 必须是 RunKind 合法值（normal/heartbeat/cron/goal），
        # 非法值在 DeepAgent._normalize_inputs 直接 ValueError → execution.error。
        # 诊断标识放 context.extra，由 session_id 前缀驱动隔离。
        "run": {
            "kind": "normal",
            "context": {"session_id": session_id, "extra": {"diagnosis": True}},
        },
    }
    if request.project_dir:
        params["project_dir"] = request.project_dir
    return e2a_from_agent_fields(
        request_id=f"diagnosis-{session_id}",
        channel_id=DIAGNOSIS_CHANNEL_ID,
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params=params,
        is_stream=True,
        timestamp=now_ts if now_ts is not None else time.time(),
    )


def map_agent_chunk(chunk: Any) -> dict[str, Any] | None:
    """AgentResponseChunk → 诊断 SSE 事件；无关事件返回 None（跳过）。

    映射口径：
    - chat.delta        → {"type": "token", "text": ...}      报告正文流
    - chat.tool_update  → {"type": "progress", "stage": "tool", "detail": ...} 工具动态
    - chat.error        → {"type": "error", "message": ...}
    - execution.error   → {"type": "error", "message": ...}   DeepAgent 循环错误
    - chat.final        → {"type": "final", "text": ...}      收尾（全文兜底）
    """
    payload = getattr(chunk, "payload", None)
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("event_type")
    if event_type == _EVENT_TOKEN:
        text = payload.get("content")
        if not isinstance(text, str) or not text:
            return None
        return {"type": "token", "text": text}
    if event_type == _EVENT_TOOL:
        tool_name = str(payload.get("tool_name") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not tool_name:
            return None
        detail = f"{tool_name}" + (f" [{status}]" if status else "")
        return {"type": "progress", "stage": "tool", "detail": detail}
    if event_type == _EVENT_ERROR:
        message = payload.get("error") or payload.get("message") or "诊断 Agent 执行失败"
        return {"type": "error", "message": str(message)}
    if event_type == _EVENT_EXECUTION_ERROR:
        message = payload.get("message") or payload.get("code") or "诊断 Agent 执行失败"
        return {"type": "error", "message": str(message)}
    if event_type == _EVENT_FINAL:
        # chat.final 常为空串（正文已在 delta 送达）；非空时作为全文兜底，
        # 由调用方决定是否在零 delta 时采用（对齐 deep 的补发语义）。
        text = payload.get("content")
        if not isinstance(text, str) or not text.strip():
            return None
        return {"type": "final", "text": text}
    return None


async def stream_diagnosis_via_agent(
    agent_client: Any,
    request: DiagnosisAgentRequest,
    *,
    session_id: str,
    now_ts: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """发信封 → 流式消费 chunk → yield 诊断事件（含首尾 progress）。

    ``agent_client.send_request_stream`` 抛错（AgentServer 不可达/断连）时
    上抛，由端点转 AGENT_UNREACHABLE —— 无降级路径，错误必须显式可见。
    """
    envelope = build_diagnosis_envelope(request, session_id=session_id, now_ts=now_ts)
    yield {"type": "progress", "stage": "agent_dispatch"}
    async for chunk in agent_client.send_request_stream(envelope):
        event = map_agent_chunk(chunk)
        if event is not None:
            yield event
