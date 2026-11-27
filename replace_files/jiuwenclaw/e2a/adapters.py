# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""
将 ACP JSON-RPC、A2A SendMessage 等外部形态转换为 E2A，并写入 provenance。

``envelope_from_a2a_send_message`` 的参数 ``metadata`` 仅对应 E2A 的 ``a2a_metadata``（A2A 规范侧），
与网关通道上的 ``metadata`` / ``channel_context`` 无关。

``e2a_response_to_acp_jsonrpc_response`` / ``e2a_response_to_a2a_stream_payload`` 将 ``E2AResponse`` 投影为
外协议形状；不适用时返回 ``None``（见 ``docs/zh/E2A-protocol.md`` §8、§12）。
"""


from __future__ import annotations

import time
import uuid as uuid_module
from typing import Any

from jiuwenclaw.e2a.constants import (
    E2A_A2A_STREAM_BRANCHES,
    E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR,
    E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
    E2A_RESPONSE_KIND_A2A_STREAM_EVENT,
    E2A_RESPONSE_KIND_E2A_ERROR,
    E2A_SOURCE_PROTOCOL_ACP,
    E2A_SOURCE_PROTOCOL_A2A,
)
from jiuwenclaw.e2a.models import (
    E2AEnvelope,
    E2AProvenance,
    E2AResponse,
    IdentityOrigin,
    merge_params_to_acp_prompt,
    utc_now_iso,
)

_CONVERTER_ACP = "jiuwenclaw.e2a.adapters:envelope_from_acp_jsonrpc"
_CONVERTER_A2A = "jiuwenclaw.e2a.adapters:envelope_from_a2a_send_message"


def envelope_from_acp_jsonrpc(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    jsonrpc_id: str | int | None = None,
    session_id: str | None = None,
    channel: str | None = None,
    identity_origin: IdentityOrigin = IdentityOrigin.USER,
    converter: str | None = None,
    extra_provenance_details: dict[str, Any] | None = None,
) -> E2AEnvelope:
    """由 ACP JSON-RPC 调用构造 E2A；provenance 标明来源为 acp。"""
    p = dict(params or {})
    sid = session_id or p.get("session_id")
    details: dict[str, Any] = {
        "kind": "jsonrpc_request",
        "jsonrpc_method": method,
    }
    if jsonrpc_id is not None:
        details["jsonrpc_id"] = jsonrpc_id
    if extra_provenance_details:
        details.update(extra_provenance_details)
    return E2AEnvelope(
        provenance=E2AProvenance(
            source_protocol=E2A_SOURCE_PROTOCOL_ACP,
            converter=converter or _CONVERTER_ACP,
            converted_at=utc_now_iso(),
            details=details,
        ),
        jsonrpc_id=jsonrpc_id,
        method=method,
        params=p,
        session_id=sid if isinstance(sid, str) else None,
        channel=channel,
        identity_origin=identity_origin,
    )


def envelope_from_a2a_send_message(
    *,
    task_id: str | None,
    context_id: str | None,
    message_body: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
    channel: str | None = None,
    identity_origin: IdentityOrigin = IdentityOrigin.USER,
    converter: str | None = None,
    extra_provenance_details: dict[str, Any] | None = None,
) -> E2AEnvelope:
    """
    将 A2A SendMessage 语义转为 E2A；provenance 标明来源为 a2a。

    默认 method 为 ``session/prompt``，完整 message/configuration 保留在 params 与 a2a_metadata。
    """
    meta = dict(metadata or {})
    accepted: list[str] = []
    if configuration:
        acc = configuration.get("acceptedOutputModes")
        if isinstance(acc, list):
            accepted = [str(x) for x in acc]
    details: dict[str, Any] = {
        "kind": "a2a_send_message",
        "abstract_operation": "SendMessage",
    }
    if extra_provenance_details:
        details.update(extra_provenance_details)
    return E2AEnvelope(
        provenance=E2AProvenance(
            source_protocol=E2A_SOURCE_PROTOCOL_A2A,
            converter=converter or _CONVERTER_A2A,
            converted_at=utc_now_iso(),
            details=details,
        ),
        method="session/prompt",
        task_id=task_id,
        context_id=context_id,
        channel=channel,
        identity_origin=identity_origin,
        expected_output_modes=accepted,
        params={"message": message_body, "configuration": configuration or {}},
        a2a_metadata=meta,
    )


def envelope_to_acp_jsonrpc_call(envelope: E2AEnvelope) -> dict[str, Any]:
    """
    将信封转为 JSON-RPC 风格单条调用描述（日志或下游 ACP 端点）。

    若 ``envelope.method`` 为网关 RPC（如 ``chat.send``），输出中的 ``method`` 对纯 ACP 端可能无效，
    需在业务层先映射为 ACP method（如 ``session/prompt``）再发送。
    """
    method = envelope.ext_method if envelope.method == "ext" and envelope.ext_method else envelope.method
    params = merge_params_to_acp_prompt(envelope) if envelope.method == "session/prompt" else dict(envelope.params)
    return {
        "jsonrpc": "2.0",
        "id": envelope.jsonrpc_id,
        "method": method,
        "params": params,
    }


def e2a_response_to_acp_jsonrpc_response(response: E2AResponse) -> dict[str, Any] | None:
    """
    将 ``E2AResponse`` 转为单条 JSON-RPC 2.0 **响应**对象（仅 ``result`` 或 ``error``，无 ``method``）。

    优先使用 ``projections.acp``：若为已组装的完整响应（含 ``jsonrpc`` 与 ``result``/``error``），原样返回副本。
    否则按 ``response_kind`` 从 ``body`` 构造：

    - ``acp.prompt_result`` → ``result`` = ``body``
    - ``acp.jsonrpc_error`` → ``error`` = ``body``（须含 JSON-RPC 所需字段）
    - ``e2a.error`` → ``error``：``code`` 非 int 时用 ``-32603``，字符串码写入 ``data.e2a_code``
    """
    proj = response.projections.get("acp") if isinstance(response.projections, dict) else None
    if isinstance(proj, dict) and proj.get("jsonrpc") == "2.0":
        if "result" in proj or "error" in proj:
            out = dict(proj)
            out.setdefault("id", response.jsonrpc_id)
            return out

    rpc_id = response.jsonrpc_id
    kind = response.response_kind
    body = dict(response.body or {})

    if kind == E2A_RESPONSE_KIND_ACP_PROMPT_RESULT:
        return {"jsonrpc": "2.0", "id": rpc_id, "result": body}

    if kind == E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": body}

    if kind == E2A_RESPONSE_KIND_E2A_ERROR:
        code_raw = body.get("code")
        code = code_raw if isinstance(code_raw, int) else -32603
        message = str(body.get("message") or "")
        data: dict[str, Any] = {}
        det = body.get("details")
        if det is not None:
            data["details"] = det
        ext = body.get("external")
        if ext is not None:
            data["external"] = ext
        if code_raw is not None and not isinstance(code_raw, int):
            data["e2a_code"] = code_raw
        err: dict[str, Any] = {"code": code, "message": message}
        if data:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rpc_id, "error": err}

    return None


def e2a_response_to_a2a_stream_payload(response: E2AResponse) -> dict[str, Any] | None:
    """
    将 ``response_kind == \"a2a.stream_event\"`` 的 ``E2AResponse`` 转为 A2A ``StreamResponse`` 形 JSON：

    外层键为 ``task`` / ``message`` / ``statusUpdate`` / ``artifactUpdate`` 之一（与常见 JSON 绑定一致）。

    若 ``projections.a2a`` 已为四选一单键对象，则原样返回副本。
    ``body.branch`` 须为 ``E2A_A2A_STREAM_BRANCHES`` 之一；``body.payload`` 为对应分支对象。
    """
    if response.response_kind != E2A_RESPONSE_KIND_A2A_STREAM_EVENT:
        return None

    proj = response.projections.get("a2a") if isinstance(response.projections, dict) else None
    if isinstance(proj, dict) and len(proj) == 1:
        key = next(iter(proj))
        if key in ("task", "message", "statusUpdate", "artifactUpdate"):
            return dict(proj)

    body = dict(response.body or {})
    branch = body.get("branch")
    payload = body.get("payload")
    if branch not in E2A_A2A_STREAM_BRANCHES or not isinstance(payload, dict):
        return None

    key_map = {
        "task": "task",
        "message": "message",
        "status_update": "statusUpdate",
        "artifact_update": "artifactUpdate",
    }
    outer = key_map.get(branch)
    if outer is None:
        return None
    return {outer: payload}


def build_acp_tool_response_message(
    jsonrpc_id: str,
    response_data: dict[str, Any],
    session_id: str | None,
    channel_id: str = "acp",
) -> Any:
    """Build an internal Message for an ACP tool response (JSON-RPC response from client).

    Shared by AcpRouteHandler (WebSocket gateway mode) and AcpChannel (stdio mode)
    to avoid duplicated Message construction logic.
    """
    from jiuwenclaw.schema.message import Message, ReqMethod

    return Message(
        id=f"acp_tool_resp_{uuid_module.uuid4().hex[:12]}",
        type="req",
        channel_id=channel_id,
        session_id=session_id,
        params={
            "jsonrpc_id": jsonrpc_id,
            "response": dict(response_data),
            "session_id": session_id,
        },
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.ACP_TOOL_RESPONSE,
        is_stream=False,
        metadata={"acp": {"jsonrpc_id": jsonrpc_id, "kind": "tool_response"}},
    )
