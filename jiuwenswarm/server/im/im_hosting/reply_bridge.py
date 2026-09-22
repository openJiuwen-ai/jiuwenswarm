"""IM 托管代回：门控通过后按托管时选定的专家经 TenantAgentPool 调 Agent。

统一入口是 ``TenantAgentPool``（按 ``expert_service_id`` / ``expert_agent_id``
取对应 Manager），relayclaw / 进程内代回共用，不要再注入个人版单例。
过程写入该专家 workspace 下 ``im_hosting.{target_id}`` 的 history.json
（user=对方原话；流式路径还会落思考/工具）。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Protocol

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import resolve_tenant_agent_workspace_dir, resolve_tenant_sessions_dir
from jiuwenswarm.server.im.im_connector.types import ImMessage

LOGGER = logging.getLogger(__name__)

HOSTING_AGENT_CHANNEL_ID = "im_hosting"


class AgentManagerLike(Protocol):
    async def process_message(self, request: AgentRequest) -> AgentResponse: ...


def hosting_session_id(target_id: str) -> str:
    """history 目录名：须过 ``is_valid_session_id``（禁止冒号）。"""
    return f"im_hosting.{target_id}"


def resolve_target_expert(target: dict[str, Any]) -> tuple[str, str]:
    # 个人版没有专家选择器，未填则固定 default/default。
    service_id = str(target.get("expert_service_id") or "").strip() or "default"
    agent_id = str(target.get("expert_agent_id") or "").strip() or "default"
    return service_id, agent_id


def resolve_reply_runtime(override: AgentManagerLike | None = None) -> AgentManagerLike:
    if override is not None:
        return override
    from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

    return TenantAgentPool.get_instance()


def expert_sessions_root(
    target: dict[str, Any],
    *,
    workspace_key: str | None = None,
) -> str:
    service_id, agent_id = resolve_target_expert(target)
    return str(
        resolve_tenant_sessions_dir(
            workspace_key,
            service_id=service_id,
            agent_id=agent_id,
        )
    )


def load_hosting_history(
    target: dict[str, Any],
    *,
    workspace_key: str | None = None,
) -> dict[str, Any]:
    """读专家 workspace 里该托管 session 的 history.json / jsonl。"""
    service_id, agent_id = resolve_target_expert(target)
    session_id = hosting_session_id(str(target["id"]))
    sessions_root = expert_sessions_root(target, workspace_key=workspace_key)
    from jiuwenswarm.server.runtime.session.session_history import (
        history_exists,
        load_history_records,
    )

    if not history_exists(session_id, sessions_root=sessions_root):
        messages: list[dict[str, Any]] = []
    else:
        messages = load_history_records(session_id, sessions_root=sessions_root)
    return {
        "session_id": session_id,
        "messages": messages,
        "expert_service_id": service_id,
        "expert_agent_id": agent_id,
    }


def inbound_display_text(message: ImMessage) -> str:
    sender = (message.sender_name or message.sender_account or "").strip()
    body = (message.content_text or "").strip()
    if sender:
        return f"{sender}: {body}" if body else sender
    return body


def build_inbound_prompt(
    message: ImMessage,
    *,
    target_title: str | None,
    target_kind: str,
    persona: str | None = None,
) -> str:
    sender = (message.sender_name or message.sender_account or "对方").strip()
    title = (target_title or message.conversation_external_id or "会话").strip()
    body = (message.content_text or "").strip()
    kind_label = "群聊" if target_kind == "group" else "私聊"
    profile = (persona or "").strip()
    profile_block = f"分身说明：\n{profile}\n\n" if profile else ""
    return (
        f"你是用户的 IM 数字分身，正在代用户回复一条{kind_label}消息。\n"
        f"{profile_block}"
        f"请直接给出可发送的回复正文：简洁、礼貌、不要 Markdown 代码块，不要解释推理过程。"
        f"语气和立场要符合上面的分身说明。\n\n"
        f"会话：{title}\n"
        f"发送者：{sender}\n"
        f"消息：{body}"
    )


def extract_reply_text(response: AgentResponse | None) -> str:
    if response is None or not getattr(response, "ok", False):
        return ""
    payload = getattr(response, "payload", None) or {}
    if not isinstance(payload, dict):
        return ""
    for key in ("content", "text", "answer"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    event = payload.get("event_type")
    if event in {"chat.final", "chat.complete"}:
        inner = payload.get("content")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return ""


def _reply_from_session_history(request: AgentRequest) -> str:
    """发送正文与右侧 history 同一条：该 request 最后一条 chat.final.content。"""
    try:
        from jiuwenswarm.server.runtime.session.session_history import load_history_records

        sessions_root = str(
            resolve_tenant_sessions_dir(
                getattr(request, "workspace_key", None),
                service_id=request.service_id,
                agent_id=request.agent_id,
            )
        )
        records = load_history_records(str(request.session_id), sessions_root=sessions_root)
    except Exception:
        LOGGER.debug("[im_hosting] read session history for reply failed", exc_info=True)
        return ""
    rid = str(request.request_id or "")
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        if rid and str(record.get("request_id") or "") != rid:
            continue
        if record.get("event_type") != "chat.final":
            continue
        text = record.get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


async def _invoke_expert(runtime: AgentManagerLike, request: AgentRequest) -> str:
    stream = getattr(runtime, "process_message_stream", None)
    if callable(stream):
        async for _chunk in stream(request):
            pass
        return _reply_from_session_history(request)
    request.is_stream = False
    response = await runtime.process_message(request)
    return extract_reply_text(response)


def resolve_target_persona(
    target: dict[str, Any],
    channel_policy: dict[str, Any] | None = None,
) -> str:
    own = str(target.get("expert_persona") or "").strip()
    if own:
        return own
    return str((channel_policy or {}).get("expert_persona") or "").strip()


async def generate_reply_via_expert(
    runtime: AgentManagerLike | None = None,
    *,
    target: dict[str, Any],
    message: ImMessage,
    workspace_key: str | None = None,
    channel_policy: dict[str, Any] | None = None,
) -> str:
    """按 target 上记录的专家经 TenantAgentPool 生成代回；history 里 user 为对方原话。"""
    service_id, agent_id = resolve_target_expert(target)
    target_id = str(target["id"])
    prompt = build_inbound_prompt(
        message,
        target_title=target.get("title"),
        target_kind=str(target.get("target_kind") or ""),
        persona=resolve_target_persona(target, channel_policy),
    )
    display = inbound_display_text(message)
    wk = (workspace_key or "").strip() or "default"
    project_dir = str(
        resolve_tenant_agent_workspace_dir(
            wk,
            service_id=service_id,
            agent_id=agent_id,
        )
    )
    request = AgentRequest(
        request_id=str(uuid.uuid4()),
        channel_id=HOSTING_AGENT_CHANNEL_ID,
        session_id=hosting_session_id(target_id),
        service_id=service_id,
        agent_id=agent_id,
        workspace_key=wk,
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": prompt,
            "content": prompt,
            "display_query": display,
            "mode": "agent",
            "project_dir": project_dir,
        },
        is_stream=True,
        timestamp=time.time(),
        metadata={
            "im_hosting": True,
            "im_hosting_target_id": target_id,
            "source_msg_id": message.msg_id,
            "expert_service_id": service_id,
            "expert_agent_id": agent_id,
        },
    )
    text = await _invoke_expert(resolve_reply_runtime(runtime), request)
    if not text:
        LOGGER.warning(
            "[im_hosting] expert %s/%s returned empty reply target=%s msg=%s",
            service_id,
            agent_id,
            target_id,
            message.msg_id,
        )
    return text
