# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""入站原始载荷 → ``AgentRequest`` 的解析段。
**本模块不发送任何东西。** 解析失败时返回一个已编码好的错误帧，由调用方经
自己的出口：``ctx.sink``或裸连接发出去。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
)
from jiuwenswarm.common.e2a.constants import E2A_WIRE_INTERNAL_METADATA_KEYS
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_USER_HISTORY_PATTERN = re.compile(
    r"(\[[^\]\n]*用户\]\s*)(.*?)(\s*\[/对话历史\])", re.DOTALL
)


def _mask_text_for_log(value: str) -> str:
    return "******" if len(value) <= 20 else f"{value[:5]}******{value[-5:]}"


def _mask_system_prompt_for_log(system_prompt: str) -> str:
    return _SYSTEM_PROMPT_USER_HISTORY_PATTERN.sub(
        lambda match: (
            f"{match.group(1)}{_mask_text_for_log(match.group(2).strip())}{match.group(3)}"
        ),
        system_prompt,
    )


def _mask_query_for_log(data: dict[str, Any]) -> dict[str, Any]:
    params = data.get("params")
    if not isinstance(params, dict):
        return data

    masked_params = dict(params)
    query = params.get("query")
    if isinstance(query, str) and query:
        masked_params["query"] = _mask_text_for_log(query)

    system_prompt = params.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt:
        masked_params["system_prompt"] = _mask_system_prompt_for_log(system_prompt)

    supplementary_info = params.get("supplementary_info")
    if isinstance(supplementary_info, str) and supplementary_info:
        masked_params["supplementary_info"] = _mask_text_for_log(supplementary_info)

    if masked_params == params:
        return data
    return {**data, "params": masked_params}


def _log_inbound_payload(raw: str | bytes, data: dict[str, Any]) -> None:
    """Log large catalog syncs as metadata while preserving other diagnostics."""
    params = data.get("params")
    if (
        data.get("req_method") == ReqMethod.SYNC_AGENTS_CONFIGS.value
        and isinstance(params, dict)
    ):
        agents = params.get("agents")
        agent_count = len(agents) if isinstance(agents, list) else None
        logger.info(
            "[AgentWebSocketServer] Inbound raw payload: <summary> "
            "request_id=%s channel_id=%s method=%s revision=%s "
            "service_id=%s agent_count=%s raw_units=%s raw_type=%s",
            data.get("request_id"),
            data.get("channel_id"),
            data.get("req_method"),
            params.get("revision"),
            params.get("service_id"),
            agent_count,
            len(raw),
            type(raw).__name__,
            extra={"user_visible": "critical"},
        )
        return

    logger.info(
        "[AgentWebSocketServer] Inbound raw payload: %s",
        _mask_query_for_log(data),
        extra={"user_visible": "critical"},
    )


def _payload_to_request(data: dict[str, Any]) -> AgentRequest:
    """将 Gateway 发送的 JSON 载荷解析为 AgentRequest."""
    req_method = data.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in E2A_WIRE_INTERNAL_METADATA_KEYS
        } or None
    # 将 app_id 注入 metadata，供 cron 路由等下游使用
    app_id = data.get("app_id")
    if app_id:
        if metadata is None:
            metadata = {}
        metadata.setdefault("app_id", app_id)

    return AgentRequest(
        request_id=data["request_id"],
        channel_id=data.get("channel_id", "web"),
        session_id=data.get("session_id"),
        chat_id=data.get("chat_id"),
        service_id=data.get("service_id"),
        agent_id=data.get("agent_id"),
        workspace_key=data.get("workspace_key"),
        req_method=req_method,
        params=data.get("params", {}),
        is_stream=data.get("is_stream", False),
        timestamp=data.get("timestamp", 0.0),
        metadata=metadata,
    )


@dataclass(frozen=True)
class ParseResult:
    """解析结果：要么拿到 ``request``，要么拿到一个待发送的 ``error_wire``。

    不必关心错误格式 —— 格式与重构前逐字一致。
    """

    request: AgentRequest | None = None
    error_wire: dict[str, Any] | None = None
    #: 供调用方记日志用的上下文（如 JSON 解码失败的原因）。
    log_context: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.request is not None


def parse_inbound(raw: str | bytes) -> ParseResult:
    """把一条入站原始载荷解析成 ``AgentRequest``。

    Args:
        raw: 原始 JSON 文本/字节。

    Returns:
        :class:`ParseResult`。``ok`` 为 False 时 ``error_wire`` 一定非空。
    """
    try:
        data = json.loads(raw)
        _log_inbound_payload(raw, data)
    except json.JSONDecodeError as e:
        return ParseResult(
            error_wire=encode_json_parse_error_wire(
                request_id="",
                channel_id="",
                message=f"JSON 解析失败: {e}",
            ),
            log_context={"json_error": str(e)},
        )

    try:
        env = E2AEnvelope.from_dict(data)
    except Exception as parse_err:  # noqa: BLE001 - 任何解析异常都回退到 legacy 载荷
        logger.warning(
            "[AgentWebSocketServer] E2A from_dict 失败，按旧载荷解析: %s",
            parse_err,
        )
        return ParseResult(request=_payload_to_request(data))

    jw = (env.channel_context or {}).get(E2A_INTERNAL_CONTEXT_KEY)
    if isinstance(jw, dict) and jw.get(E2A_FALLBACK_FAILED_KEY):
        legacy = jw.get(E2A_LEGACY_AGENT_REQUEST_KEY)
        logger.warning(
            "[E2A][fallback] using legacy_agent_request request_id=%s",
            env.request_id,
        )
        if not isinstance(legacy, dict):
            raise ValueError("legacy_agent_request missing or not a dict")
        return ParseResult(request=_payload_to_request(legacy))

    logger.info(
        "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
        env.request_id,
        env.channel,
        env.method,
        env.is_stream,
    )
    try:
        return ParseResult(request=e2a_to_agent_request(env))
    except ValueError:
        logger.warning(
            "[E2A][compat] unknown E2A method=%r request_id=%s — replying error",
            env.method,
            env.request_id,
        )
        err_resp = AgentResponse(
            request_id=env.request_id or "",
            channel_id=env.channel or "web",
            ok=False,
            payload={"error": f"unknown method: {env.method}", "code": "UNKNOWN_METHOD"},
        )
        return ParseResult(
            error_wire=encode_agent_response_for_wire(
                err_resp, response_id=env.request_id or ""
            )
        )
