# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway：Channel Message / 类 Agent 请求字段 → E2AEnvelope；AgentResponse/Chunk → E2AResponse；规范化失败时构造兜底信封。"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from jiuwenswarm.common.e2a.constants import (
    E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST,
    E2A_RESPONSE_KIND_CRON,
    E2A_RESPONSE_KIND_PLAN_APPROVAL_REQUIRED,
    E2A_RESPONSE_KIND_E2A_CHUNK,
    E2A_RESPONSE_KIND_E2A_COMPLETE,
    E2A_RESPONSE_KIND_E2A_ERROR,
    E2A_RESPONSE_STATUS_FAILED,
    E2A_RESPONSE_STATUS_IN_PROGRESS,
    E2A_RESPONSE_STATUS_SUCCEEDED,
    E2A_SOURCE_PROTOCOL_E2A,
)
from jiuwenswarm.common.e2a.models import (
    E2AEnvelope,
    E2A_PROTOCOL_VERSION,
    E2AProvenance,
    E2AResponse,
    IdentityOrigin,
    utc_now_iso,
)

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
    from jiuwenswarm.common.schema.message import Message, ReqMethod

logger = logging.getLogger(__name__)

# 与 E2A-AgentRequest-log-migration.md §7.4 一致；通道业务勿占用此前缀。
E2A_INTERNAL_CONTEXT_KEY = "_jiuwenswarm"
E2A_FALLBACK_FAILED_KEY = "normalize_failed"
E2A_LEGACY_AGENT_REQUEST_KEY = "legacy_agent_request"
MAX_LEGACY_AGENT_REQUEST_JSON_BYTES = 512_000


def message_to_legacy_agent_dict(msg: "Message") -> dict[str, Any]:
    """从 Message 生成与历史 WebSocket 一致的 dict（用于兜底 legacy_agent_request）。"""
    rm = msg.req_method
    rm_val: str | None
    if rm is None:
        rm_val = None
    elif hasattr(rm, "value"):
        rm_val = str(rm.value)
    else:
        rm_val = str(rm)
    out: dict[str, Any] = {
        "request_id": msg.id,
        "channel_id": msg.channel_id,
        "session_id": msg.session_id,
        "user_id": msg.user_id,
        "req_method": rm_val,
        "params": dict(msg.params or {}),
        "is_stream": bool(msg.is_stream),
        "timestamp": float(msg.timestamp),
    }
    if msg.metadata:
        out["metadata"] = dict(msg.metadata)
    return out


def _legacy_payload_within_limit(legacy: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.dumps(legacy, ensure_ascii=False, default=str)
    except Exception:
        return {
            "request_id": str(legacy.get("request_id", "")),
            "channel_id": str(legacy.get("channel_id", "")),
            "session_id": legacy.get("session_id"),
            "req_method": None,
            "params": {"_e2a_fallback_error": "legacy not json-serializable"},
            "is_stream": False,
            "timestamp": 0.0,
        }
    if len(raw.encode("utf-8")) <= MAX_LEGACY_AGENT_REQUEST_JSON_BYTES:
        return legacy
    logger.error(
        "[E2A][fallback] legacy_agent_request exceeds %s bytes, stripping params",
        MAX_LEGACY_AGENT_REQUEST_JSON_BYTES,
    )
    slim = {**legacy, "params": {"_e2a_fallback_error": "legacy payload too large"}}
    return slim


def build_fallback_e2a(legacy: dict[str, Any]) -> E2AEnvelope:
    """规范化失败时仍发 E2A 形状：在 channel_context 内携带 legacy 快照。"""
    legacy = _legacy_payload_within_limit(dict(legacy))
    rid = str(legacy.get("request_id") or "")
    internal = {
        E2A_FALLBACK_FAILED_KEY: True,
        E2A_LEGACY_AGENT_REQUEST_KEY: legacy,
    }
    cc: dict[str, Any] = {E2A_INTERNAL_CONTEXT_KEY: internal}
    return E2AEnvelope(
        protocol_version=E2A_PROTOCOL_VERSION,
        request_id=rid or None,
        channel=str(legacy.get("channel_id") or "") or None,
        session_id=legacy.get("session_id"),
        user_id=legacy.get("user_id"),
        method=None,
        params={},
        is_stream=bool(legacy.get("is_stream", False)),
        channel_context=cc,
    )


def message_to_e2a(msg: "Message") -> E2AEnvelope:
    """Message → E2AEnvelope（不经兜底）。"""
    d: dict[str, Any] = {
        "request_id": msg.id,
        "channel_id": msg.channel_id,
        "session_id": msg.session_id,
        "user_id": msg.user_id,
        "chat_id": msg.chat_id,
        "params": dict(msg.params or {}),
        "is_stream": bool(msg.is_stream),
        "timestamp": msg.timestamp,
    }
    if msg.req_method is not None:
        d["method"] = msg.req_method.value
    # 合并 metadata 和独立字段（enable_memory, group_digital_avatar 等）
    metadata: dict[str, Any] = dict(msg.metadata or {})

    # enable_memory 逻辑：只有当 enable_memory=False 且 group_digital_avatar=True 且 is_group_chat=True 时才禁用记忆
    # is_group_chat 通过 metadata 中的 avatar_mode 判断
    is_group_chat = bool(metadata.get("avatar_mode", False))
    should_disable_memory = (
        msg.enable_memory is False  # 配置中明确设置为 false
        and msg.group_digital_avatar is True  # 数字分身模式
        and is_group_chat is True  # 群聊消息
    )
    # 默认启用记忆，只有在上述三个条件同时满足时才禁用
    final_enable_memory = not should_disable_memory
    # 只有当 msg.enable_memory 不为 None 时，才将 enable_memory 写入 metadata
    if msg.enable_memory is not None:
        metadata["enable_memory"] = final_enable_memory
    logger.info(
        "[E2A][enable_memory] msg.enable_memory=%s msg.group_digital_avatar=%s "
        "is_group_chat=%s should_disable=%s final=%s",
        msg.enable_memory, msg.group_digital_avatar, is_group_chat,
        should_disable_memory, final_enable_memory
    )

    if msg.group_digital_avatar:
        metadata["group_digital_avatar"] = msg.group_digital_avatar
    if metadata:
        d["metadata"] = metadata

    # ── V2: 传递 agent_ref / app_id 到 E2A 线协议 ──
    if getattr(msg, "agent_ref", None):
        d["agent_ref"] = msg.agent_ref
    if getattr(msg, "app_id", None):
        d["app_id"] = msg.app_id

    return E2AEnvelope.from_dict(d)


def message_to_e2a_or_fallback(msg: "Message") -> E2AEnvelope:
    """Message → E2A；失败或校验不通过则 build_fallback_e2a。"""
    try:
        env = message_to_e2a(msg)
        if not (env.request_id and str(env.request_id).strip()):
            raise ValueError("empty request_id")
        if env.params is not None and not isinstance(env.params, dict):
            raise ValueError("params must be dict")
        logger.info(
            "[E2A][norm] request_id=%s channel=%s method=%s is_stream=%s params_keys=%s",
            env.request_id,
            env.channel,
            env.method,
            env.is_stream,
            list((env.params or {}).keys())[:32],
        )
        return env
    except Exception as e:
        legacy = message_to_legacy_agent_dict(msg)
        logger.warning(
            "[E2A][fallback] normalize failed request_id=%s err=%s",
            getattr(msg, "id", ""),
            e,
        )
        return build_fallback_e2a(legacy)


def e2a_from_agent_fields(
    *,
    request_id: str,
    channel_id: str = "",
    session_id: str | None = None,
    req_method: "ReqMethod | str | None" = None,
    params: dict[str, Any] | None = None,
    is_stream: bool = False,
    timestamp: float = 0.0,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> E2AEnvelope:
    """由与 AgentRequest 相同的字段构造 E2A（heartbeat / cron / app 管理请求等）。"""
    d: dict[str, Any] = {
        "request_id": request_id,
        "channel_id": channel_id,
        "session_id": session_id,
        "user_id": user_id,
        "params": dict(params or {}),
        "is_stream": is_stream,
        "timestamp": timestamp,
    }
    if req_method is not None:
        if hasattr(req_method, "value"):
            d["method"] = req_method.value
        else:
            d["method"] = str(req_method)
    if metadata:
        d["metadata"] = dict(metadata)
    return E2AEnvelope.from_dict(d)


def channel_context_for_channel_reply(env: E2AEnvelope) -> dict[str, Any] | None:
    """供流式 chunk 回传到 Channel：去掉内部 _jiuwenswarm，保留 trace 与业务 metadata。"""
    ctx = dict(env.channel_context or {})
    ctx.pop(E2A_INTERNAL_CONTEXT_KEY, None)
    return ctx if ctx else None


def e2a_response_from_agent_response(
    resp: "AgentResponse",
    *,
    response_id: str,
    sequence: int = 0,
    timestamp: str | None = None,
) -> E2AResponse:
    """
    将 ``AgentResponse``（与 ``E:\\logs`` 中 ``AgentResponse: {...}`` 同形）规范为 ``E2AResponse``。

    非流式完整响应恒为 ``is_final=True``、``sequence`` 默认 0。
    """
    from jiuwenswarm.common.schema.agent import AgentResponse as AgentResponseCls

    if not isinstance(resp, AgentResponseCls):
        raise TypeError("resp must be AgentResponse")

    ts = timestamp or utc_now_iso()
    prov = E2AProvenance(
        source_protocol=E2A_SOURCE_PROTOCOL_E2A,
        converter="jiuwenswarm.common.e2a.gateway_normalize:e2a_response_from_agent_response",
        converted_at=ts,
        details={"kind": "legacy_agent_response", "ok": bool(resp.ok)},
    )
    meta = dict(resp.metadata) if resp.metadata else {}

    if resp.ok:
        body: dict[str, Any] = {"result": dict(resp.payload) if resp.payload else {}}
        status = E2A_RESPONSE_STATUS_SUCCEEDED
        kind = E2A_RESPONSE_KIND_E2A_COMPLETE
    else:
        pl = dict(resp.payload) if resp.payload else {}
        err = pl.get("error", pl.get("message", "Agent error"))
        body = {
            "code": "E2A.AGENT_ERROR",
            "message": str(err),
            "details": pl,
        }
        status = E2A_RESPONSE_STATUS_FAILED
        kind = E2A_RESPONSE_KIND_E2A_ERROR

    return E2AResponse(
        protocol_version=E2A_PROTOCOL_VERSION,
        response_id=response_id,
        request_id=resp.request_id,
        sequence=sequence,
        is_final=True,
        status=status,
        response_kind=kind,
        timestamp=ts,
        provenance=prov,
        body=body,
        channel=resp.channel_id or None,
        metadata=meta,
        identity_origin=IdentityOrigin.AGENT,
        is_stream=False,
        # V2: 非流式响应回带 agent_ref，与流式 chunk 对称（设计 §6.3）。
        agent_ref=getattr(resp, "agent_ref", None),
    )


def e2a_response_from_agent_chunk(
    chunk: "AgentResponseChunk",
    *,
    response_id: str,
    sequence: int,
    is_stream: bool = True,
    timestamp: str | None = None,
) -> E2AResponse:
    """
    将 ``AgentResponseChunk``（与 ``E:\\logs`` 中 ``AgentResponseChunk: {...}`` 同形）规范为 ``E2AResponse``。

    - 中间帧：``e2a.chunk``，``status=in_progress``，``is_final=False``。
    - 终止帧（如 ``payload == {\"is_complete\": true}``）：``e2a.complete``。
    - ``chat.delta``：按 ``source_chunk_type`` 映射 ``body.delta_kind``（``llm_reasoning`` → ``reasoning``）。
    """
    from jiuwenswarm.common.schema.agent import AgentResponseChunk as AgentResponseChunkCls

    if not isinstance(chunk, AgentResponseChunkCls):
        raise TypeError("chunk must be AgentResponseChunk")

    ts = timestamp or utc_now_iso()
    prov = E2AProvenance(
        source_protocol=E2A_SOURCE_PROTOCOL_E2A,
        converter="jiuwenswarm.common.e2a.gateway_normalize:e2a_response_from_agent_chunk",
        converted_at=ts,
        details={"kind": "legacy_agent_response_chunk", "is_complete": chunk.is_complete},
    )
    pl = dict(chunk.payload) if chunk.payload else {}

    # ── 从 chunk 提取 agent_ref / metadata（V2 路由用）──
    _chunk_agent_ref: dict | None = getattr(chunk, "agent_ref", None)
    _chunk_metadata: dict = getattr(chunk, "metadata", None) or {}

    if chunk.is_complete and pl == {"is_complete": True}:
        return E2AResponse(
            protocol_version=E2A_PROTOCOL_VERSION,
            response_id=response_id,
            request_id=chunk.request_id,
            sequence=sequence,
            is_final=True,
            status=E2A_RESPONSE_STATUS_SUCCEEDED,
            response_kind=E2A_RESPONSE_KIND_E2A_COMPLETE,
            timestamp=ts,
            provenance=prov,
            body={"result": {}},
            channel=chunk.channel_id or None,
            identity_origin=IdentityOrigin.AGENT,
            is_stream=is_stream,
            agent_ref=_chunk_agent_ref,
            metadata=_chunk_metadata,
        )

    if chunk.is_complete and pl.get("event_type") == "chat.error":
        return E2AResponse(
            protocol_version=E2A_PROTOCOL_VERSION,
            response_id=response_id,
            request_id=chunk.request_id,
            sequence=sequence,
            is_final=True,
            status=E2A_RESPONSE_STATUS_FAILED,
            response_kind=E2A_RESPONSE_KIND_E2A_ERROR,
            timestamp=ts,
            provenance=prov,
            body={
                "code": "chat.error",
                "message": str(pl.get("error", "")),
                "details": pl,
            },
            channel=chunk.channel_id or None,
            identity_origin=IdentityOrigin.AGENT,
            is_stream=is_stream,
            agent_ref=_chunk_agent_ref,
            metadata=_chunk_metadata,
        )

    if chunk.is_complete:
        return E2AResponse(
            protocol_version=E2A_PROTOCOL_VERSION,
            response_id=response_id,
            request_id=chunk.request_id,
            sequence=sequence,
            is_final=True,
            status=E2A_RESPONSE_STATUS_SUCCEEDED,
            response_kind=E2A_RESPONSE_KIND_E2A_COMPLETE,
            timestamp=ts,
            provenance=prov,
            body={"result": pl},
            channel=chunk.channel_id or None,
            identity_origin=IdentityOrigin.AGENT,
            is_stream=is_stream,
            agent_ref=_chunk_agent_ref,
            metadata=_chunk_metadata,
        )

    event_type = pl.get("event_type")
    if event_type == "chat.delta":
        sct = pl.get("source_chunk_type")
        delta_kind = "reasoning" if sct == "llm_reasoning" else "text"
        body_chunk: dict[str, Any] = {
            "delta_kind": delta_kind,
            "delta": pl.get("content", ""),
            "event_type": event_type,
            "source_chunk_type": sct,
        }
        # Preserve team-member attribution fields for frontend display
        for _key in ("role", "member_name"):
            _val = pl.get(_key)
            if _val is not None:
                body_chunk[_key] = _val
    else:
        body_chunk = {
            "delta_kind": "custom",
            "delta": pl,
            "event_type": event_type,
        }

    return E2AResponse(
        protocol_version=E2A_PROTOCOL_VERSION,
        response_id=response_id,
        request_id=chunk.request_id,
        sequence=sequence,
        is_final=False,
        status=E2A_RESPONSE_STATUS_IN_PROGRESS,
        response_kind=E2A_RESPONSE_KIND_E2A_CHUNK,
        timestamp=ts,
        provenance=prov,
        body=body_chunk,
        channel=chunk.channel_id or None,
        identity_origin=IdentityOrigin.AGENT,
        is_stream=is_stream,
        agent_ref=_chunk_agent_ref,
        metadata=_chunk_metadata,
    )


def e2a_response_to_agent_response(e2a: E2AResponse) -> "AgentResponse":
    """
    ``E2AResponse`` → 非流式 ``AgentResponse``（与 ``e2a_response_from_agent_response`` 对仗）。

    仅处理网关 unary 常见 ``response_kind``：``e2a.complete``、``e2a.error``；其它 kind 抛 ``ValueError``。
    """
    from jiuwenswarm.common.schema.agent import AgentResponse

    rid = str(e2a.request_id or "")
    ch = str(e2a.channel or "")
    meta = dict(e2a.metadata) if e2a.metadata else None
    kind = e2a.response_kind
    body = dict(e2a.body or {})

    if kind == E2A_RESPONSE_KIND_E2A_COMPLETE and e2a.status == E2A_RESPONSE_STATUS_SUCCEEDED:
        res = body.get("result")
        pl = dict(res) if isinstance(res, dict) else {}
        return AgentResponse(
            request_id=rid,
            channel_id=ch,
            ok=True,
            payload=pl,
            metadata=meta,
        )

    if kind == E2A_RESPONSE_KIND_E2A_ERROR or e2a.status == E2A_RESPONSE_STATUS_FAILED:
        details = body.get("details")
        if isinstance(details, dict):
            pl = dict(details)
        else:
            pl = {
                "error": body.get("message", "Agent error"),
                "code": body.get("code"),
            }
        return AgentResponse(
            request_id=rid,
            channel_id=ch,
            ok=False,
            payload=pl,
            metadata=meta,
        )

    raise ValueError(
        f"e2a_response_to_agent_response: unsupported response_kind={kind!r} status={e2a.status!r}. "
        "Streaming frames (e2a.chunk / in_progress) must use parse_agent_server_wire_chunk / send_request_stream. "
        "If unexpected, check outbound envelope is_stream and duplicate in-flight request_id on the WebSocket client."
    )


def e2a_response_to_agent_chunk(e2a: E2AResponse) -> "AgentResponseChunk":
    """
    ``E2AResponse`` → ``AgentResponseChunk``（与 ``e2a_response_from_agent_chunk`` 对仗）。

    覆盖：流式 ``e2a.chunk``、终止 ``e2a.complete`` / ``e2a.error``（含 ``chat.error`` 形 ``details``）。
    """
    from jiuwenswarm.common.schema.agent import AgentResponseChunk

    rid = str(e2a.request_id or "")
    ch = str(e2a.channel or "")
    body = dict(e2a.body or {})
    kind = e2a.response_kind

    # ── V2: 从 E2A 响应提取 agent_ref / metadata ──
    _agent_ref: dict | None = getattr(e2a, "agent_ref", None) or None
    _meta: dict | None = dict(e2a.metadata) if e2a.metadata else None

    if kind == E2A_RESPONSE_KIND_E2A_COMPLETE and e2a.is_final:
        res = body.get("result")

        def _empty_complete_marker(b: dict[str, Any], r: Any) -> bool:
            if b == {"result": {}}:
                return True
            if not isinstance(r, dict) or r:
                return False
            return list(b.keys()) == ["result"]

        if _empty_complete_marker(body, res):
            return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
                request_id=rid,
                channel_id=ch,
                payload={"is_complete": True},
                is_complete=True,
            )
        pl = dict(res) if isinstance(res, dict) else {}
        return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
            request_id=rid,
            channel_id=ch,
            payload=pl,
            is_complete=True,
        )

    if kind == E2A_RESPONSE_KIND_E2A_ERROR and e2a.is_final:
        det = body.get("details")
        pl = dict(det) if isinstance(det, dict) else dict(body)
        return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
            request_id=rid,
            channel_id=ch,
            payload=pl,
            is_complete=True,
        )

    if kind == E2A_RESPONSE_KIND_E2A_CHUNK and not e2a.is_final:
        dk = body.get("delta_kind")
        et = body.get("event_type")
        delta = body.get("delta")
        sct_in = body.get("source_chunk_type")

        if et == "chat.delta" or dk in ("text", "reasoning"):
            sct = "llm_reasoning" if dk == "reasoning" else sct_in
            pl: dict[str, Any] = {
                "event_type": "chat.delta",
                "content": delta if delta is not None else "",
            }
            if sct is not None:
                pl["source_chunk_type"] = sct
            # Preserve team-member attribution fields for frontend display
            for _key in ("role", "member_name"):
                _val = body.get(_key)
                if _val is not None:
                    pl[_key] = _val
            return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
                request_id=rid,
                channel_id=ch,
                payload=pl,
                is_complete=False,
            )

        if isinstance(delta, dict):
            pl2 = dict(delta)
        else:
            pl2 = {}
            if et is not None:
                pl2["event_type"] = et
            if delta is not None:
                pl2["content"] = delta
        if et is not None and "event_type" not in pl2:
            pl2["event_type"] = et
        return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
            request_id=rid,
            channel_id=ch,
            payload=pl2,
            is_complete=False,
        )

    if kind == E2A_RESPONSE_KIND_CRON:
        body_payload = {
            "event_type": "cron.response",
            "action": body.get("action"),
            "status": body.get("status"),
            "data": body.get("data"),
            "message": body.get("message"),
        }
        return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
            request_id=rid,
            channel_id=ch,
            payload=body_payload,
            is_complete=True,
        )

    if kind == E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST:
        return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
            request_id=rid,
            channel_id=ch,
            payload={
                "event_type": "acp.output_request",
                "jsonrpc": dict(body),
            },
            is_complete=False,
        )

    if kind == E2A_RESPONSE_KIND_PLAN_APPROVAL_REQUIRED:
        return AgentResponseChunk(agent_ref=_agent_ref, metadata=_meta, 
            request_id=rid,
            channel_id=ch,
            payload={
                "event_type": "plan.approval_required",
                "plan_content": body.get("plan_content", ""),
                "plan_slug": body.get("plan_slug", ""),
                "plan_path": body.get("plan_path", ""),
                "session_id": e2a.session_id or "",
            },
            is_complete=True,
        )

    raise ValueError(
        f"e2a_response_to_agent_chunk: unsupported response_kind={kind!r} is_final={e2a.is_final!r}"
    )
