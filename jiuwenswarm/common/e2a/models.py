# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""
E2A 数据模型：请求信封 ``E2AEnvelope``、响应 ``E2AResponse`` 与子结构。

完整约定、易混点与 JSON 示例见仓库 ``docs/zh/E2A-protocol.md``（``docs/en/E2A-protocol.md``）。字段以本模块 dataclass 为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from jiuwenswarm.common.e2a.constants import (
    E2A_RESPONSE_STATUS_IN_PROGRESS,
    E2A_SOURCE_PROTOCOL_A2A,
    E2A_SOURCE_PROTOCOL_ACP,
    E2A_SOURCE_PROTOCOL_E2A,
    E2A_WIRE_INTERNAL_METADATA_KEYS,
)


E2A_PROTOCOL_VERSION = "1.0"


def utc_now_iso() -> str:
    """当前 UTC 时刻的 RFC 3339 字符串（``provenance.converted_at``、响应 ``timestamp`` 缺省等）。"""

    return datetime.now(timezone.utc).isoformat()


class IdentityOrigin(str, Enum):
    """身份来源：谁触发了本次对 Agent 的请求。"""

    SYSTEM = "system"
    USER = "user"
    AGENT = "agent"
    SERVICE = "service"


@dataclass
class E2AProvenance:
    """
    记录 E2A 信封的出处。

    - E2A 为统一载体：ACP、A2A 等消息经转换后均应落在此结构中。
    - ``source_protocol`` 标明**进入 E2A 之前**所依据的主要协议或「原生 E2A」。
    - ``converter`` / ``converted_at`` / ``details`` 标明由谁、何时、从何种具体调用转换而来。
    """

    source_protocol: str = E2A_SOURCE_PROTOCOL_E2A
    converter: str | None = None
    converted_at: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class E2AFileRef:
    """文件引用（用于 ``params.files`` / ``params.attachments`` 等元素，对齐 MCP/A2A 常见形态）。"""

    uri: str
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    _meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class E2AAuth:
    """
    身份鉴权信息（按需填充）。

    建议：生产环境用 credential_ref / oauth 等间接引用，由网关在受控环境换票。
    """

    method_id: str | None = None
    bearer_token: str | None = None
    api_key_ref: str | None = None
    credential_ref: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    _meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class E2AEnvelope:
    """
    E2A 统一信封：单结构兼容多协议入口，由网关或适配层解析后调用 Agent。

    基础字段：
    - protocol_version：E2A 载荷版本。
    - provenance：出处（原生 e2a 或由 acp / a2a 等转换）。
    - request_id：网关↔AgentServer 主请求 id（流式 chunk 关联）。
    - jsonrpc_id / correlation_id：JSON-RPC id、分布式追踪等（可与 request_id 并存）。
    - task_id / context_id / session_id / message_id：对齐 A2A / ACP 侧概念。
    - is_stream：是否流式响应。

    事件语义：
    - method：**网关 RPC**（如 ``chat.send``）或 ACP 转入时的 JSON-RPC method；``ext`` + ``ext_method`` 用于自定义。
    - **params**：**唯一业务参数字典**（JSON-RPC params、用户正文、``content_blocks``、附件列表等均放此处，见仓库 ``docs/zh/E2A-protocol.md``）。

    通道与互操作：
    - channel_context：**可选溢出**；主路径上通道侧信息应在**网关入口**映射为规范化字段。
    - a2a_metadata / acp_meta：与 A2A/ACP 互操作时使用。
    """

    # --- 基础 / 关联 ---
    protocol_version: str = E2A_PROTOCOL_VERSION
    provenance: E2AProvenance = field(default_factory=E2AProvenance)
    request_id: str | None = None
    jsonrpc_id: str | int | None = None
    correlation_id: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    session_id: str | None = None
    message_id: str | None = None

    # --- 时间戳：规范为 RFC 3339 UTC 字符串；from_dict 可将历史 float 纪元秒规范化 ---
    timestamp: str | None = None

    # --- 身份与入口 ---
    identity_origin: IdentityOrigin = IdentityOrigin.USER
    channel: str | None = None
    user_id: str | None = None
    agent_ref: dict | None = None
    chat_id: str | None = None
    source_agent_id: str | None = None

    # --- 网关 RPC（原 req_method）；ACP 转入时同字段承载 JSON-RPC method ---
    method: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    ext_method: str | None = None
    session_update_kind: str | None = None
    is_stream: bool = False

    # --- 期望输出（对齐 A2A acceptedOutputModes）---
    expected_output_modes: list[str] = field(default_factory=list)

    # --- 鉴权 ---
    auth: E2AAuth | None = None

    # --- 扩展槽 ---
    channel_context: dict[str, Any] = field(default_factory=dict)
    a2a_metadata: dict[str, Any] = field(default_factory=dict)
    acp_meta: dict[str, Any] = field(default_factory=dict)

    def ensure_timestamp(self) -> None:
        """若未设置 timestamp，则填当前 UTC ISO8601。"""
        if self.timestamp is None:
            self.timestamp = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好 dict（枚举转为值）。"""
        d = _dataclass_to_json_dict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> E2AEnvelope:
        return _envelope_from_dict(data)


@dataclass
class E2AResponse:
    """
    E2A 统一响应：每条出站记录（含流式多帧）一条实例；与 ``E2AEnvelope`` 对称。

    分层语义见 ``docs/zh/E2A-protocol.md`` / ``docs/en/E2A-protocol.md`` §12；``response_kind`` 取值以
    ``constants.E2A_RESPONSE_KINDS`` 为准。

    ``metadata``：通道/业务自定义键值；兼容旧版 ``AgentResponse.metadata``；协议转换失败时可临时写入兜底信息
    （如原始片段、错误说明），与 ``a2a_metadata`` / ``acp_meta`` 分工不同。
    """

    protocol_version: str = E2A_PROTOCOL_VERSION
    response_id: str | None = None
    request_id: str | None = None
    sequence: int = 0
    is_final: bool = False
    status: str = E2A_RESPONSE_STATUS_IN_PROGRESS
    response_kind: str = ""
    timestamp: str | None = None
    provenance: E2AProvenance = field(default_factory=E2AProvenance)
    body: dict[str, Any] = field(default_factory=dict)

    jsonrpc_id: str | int | None = None
    correlation_id: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    is_stream: bool = False
    identity_origin: IdentityOrigin = IdentityOrigin.AGENT
    channel: str | None = None
    user_id: str | None = None
    agent_ref: dict | None = None
    source_agent_id: str | None = None
    method: str | None = None

    projections: dict[str, Any] = field(default_factory=dict)
    channel_context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    a2a_metadata: dict[str, Any] = field(default_factory=dict)
    acp_meta: dict[str, Any] = field(default_factory=dict)

    def ensure_timestamp(self) -> None:
        """若未设置 timestamp，则填当前 UTC ISO8601。"""
        if self.timestamp is None:
            self.timestamp = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好 dict（枚举转为值）。"""
        return _dataclass_to_json_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> E2AResponse:
        return _e2a_response_from_dict(data)


def _enum_value(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    return obj


def _dataclass_to_json_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "__dataclass_fields__"):
        out: dict[str, Any] = {}
        for f in fields(obj):
            v = getattr(obj, f.name)
            if v is None and f.name.startswith("_"):
                continue
            key = f.name
            if isinstance(v, Enum):
                out[key] = v.value
            elif hasattr(v, "__dataclass_fields__"):
                out[key] = _dataclass_to_json_dict(v)
            elif isinstance(v, list):
                out[key] = [
                    _dataclass_to_json_dict(x)
                    if hasattr(x, "__dataclass_fields__")
                    else _enum_value(x)
                    for x in v
                ]
            elif isinstance(v, dict):
                out[key] = {
                    k: _dataclass_to_json_dict(x)
                    if hasattr(x, "__dataclass_fields__")
                    else x
                    for k, x in v.items()
                }
            else:
                out[key] = v
        return out
    return obj


def _provenance_from_dict(raw: Any) -> E2AProvenance:
    if raw is None:
        return E2AProvenance()
    if isinstance(raw, E2AProvenance):
        return raw
    if not isinstance(raw, dict):
        return E2AProvenance()
    return E2AProvenance(
        source_protocol=str(raw.get("source_protocol", E2A_SOURCE_PROTOCOL_E2A)),
        converter=raw.get("converter"),
        converted_at=raw.get("converted_at"),
        details=dict(raw.get("details") or {}),
    )


def _normalize_timestamp_value(raw: Any) -> str | None:
    """规范为 RFC 3339 UTC 字符串；接受 str 或历史 float/int 纪元秒。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
    return str(raw)


def _migrate_legacy_binding(data: dict[str, Any], prov: E2AProvenance) -> E2AProvenance:
    """旧版 ``binding`` 字段迁入 provenance.details，避免丢失信息。"""
    legacy = data.get("binding")
    if legacy is None or prov.details.get("migrated_from_binding") is not None:
        return prov
    if isinstance(legacy, dict) and "value" in legacy:
        legacy = legacy["value"]
    legacy_s = str(legacy) if legacy is not None else ""
    d = dict(prov.details)
    d["migrated_from_binding"] = legacy_s
    sp = prov.source_protocol
    if sp == E2A_SOURCE_PROTOCOL_E2A:
        if legacy_s == E2A_SOURCE_PROTOCOL_ACP:
            sp = E2A_SOURCE_PROTOCOL_ACP
        elif legacy_s == E2A_SOURCE_PROTOCOL_A2A:
            sp = E2A_SOURCE_PROTOCOL_A2A
        elif legacy_s in ("internal", "hybrid"):
            sp = E2A_SOURCE_PROTOCOL_E2A
    return E2AProvenance(
        source_protocol=sp,
        converter=prov.converter,
        converted_at=prov.converted_at,
        details=d,
    )


def _params_with_optional_legacy_payload(data: dict[str, Any]) -> dict[str, Any]:
    """
    以 ``params`` 为真源；若存在顶层 ``payload`` 对象，将其键合并进 params（不覆盖已有键）。
    """
    p = dict(data.get("params") or {})
    raw = data.get("payload")
    if not isinstance(raw, dict) or not raw:
        return p
    for k, v in raw.items():
        if k in p:
            continue
        if v is None:
            continue
        if v == [] or v == {}:
            continue
        p[k] = v
    return p


def _envelope_from_dict(data: dict[str, Any]) -> E2AEnvelope:
    prov = _provenance_from_dict(data.get("provenance"))
    prov = _migrate_legacy_binding(data, prov)

    origin = data.get("identity_origin", IdentityOrigin.USER.value)
    if isinstance(origin, str):
        origin = IdentityOrigin(origin)

    params = _params_with_optional_legacy_payload(data)

    auth_raw = data.get("auth")
    auth: E2AAuth | None
    if auth_raw is None:
        auth = None
    elif isinstance(auth_raw, E2AAuth):
        auth = auth_raw
    else:
        auth = E2AAuth(
            method_id=auth_raw.get("method_id"),
            bearer_token=auth_raw.get("bearer_token"),
            api_key_ref=auth_raw.get("api_key_ref"),
            credential_ref=auth_raw.get("credential_ref"),
            extra_headers=dict(auth_raw.get("extra_headers") or {}),
            _meta=dict(auth_raw.get("_meta") or {}),
        )

    # channel_context：合并 wire 顶层 metadata + app_id 中尚未出现的键。
    channel_context = dict(data.get("channel_context") or {})
    meta_top = data.get("metadata")
    if isinstance(meta_top, dict) and meta_top:
        for k, v in meta_top.items():
            if k in E2A_WIRE_INTERNAL_METADATA_KEYS:
                continue
            if k not in channel_context:
                channel_context[k] = v
    app_id = data.get("app_id")
    if app_id and "app_id" not in channel_context:
        channel_context["app_id"] = app_id

    ch = data.get("channel")
    if ch is None:
        ch = data.get("channel_id")

    raw_method = data.get("method")
    if raw_method is None and "req_method" in data:
        rm = data["req_method"]
        if isinstance(rm, str):
            raw_method = rm
        elif hasattr(rm, "value"):
            raw_method = str(rm.value)

    return E2AEnvelope(
        protocol_version=data.get("protocol_version", E2A_PROTOCOL_VERSION),
        provenance=prov,
        request_id=data.get("request_id"),
        jsonrpc_id=data.get("jsonrpc_id"),
        correlation_id=data.get("correlation_id"),
        task_id=data.get("task_id"),
        context_id=data.get("context_id"),
        session_id=data.get("session_id"),
        message_id=data.get("message_id"),
        timestamp=_normalize_timestamp_value(data.get("timestamp")),
        identity_origin=origin,
        channel=ch,
        user_id=data.get("user_id"),
        # V2: agent_ref 全链路透传——入站反序列化须补读，否则 message_to_e2a
        # 写入 d["agent_ref"] 后被 from_dict 丢弃，AgentServer 永远收不到
        # （设计 §6.3 agent_ref 全程携带；§5.2 同人多窗 via agent_ref）。
        agent_ref=data.get("agent_ref"),
        chat_id=data.get("chat_id"),
        source_agent_id=data.get("source_agent_id"),
        method=raw_method,
        params=params,
        ext_method=data.get("ext_method"),
        session_update_kind=data.get("session_update_kind"),
        is_stream=bool(data.get("is_stream", False)),
        expected_output_modes=list(data.get("expected_output_modes") or []),
        auth=auth,
        channel_context=channel_context,
        a2a_metadata=dict(data.get("a2a_metadata") or {}),
        acp_meta=dict(data.get("acp_meta") or {}),
    )


def _e2a_response_from_dict(data: dict[str, Any]) -> E2AResponse:
    prov = _provenance_from_dict(data.get("provenance"))

    origin = data.get("identity_origin", IdentityOrigin.AGENT.value)
    if isinstance(origin, str):
        origin = IdentityOrigin(origin)

    seq_raw = data.get("sequence", 0)
    try:
        sequence = int(seq_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        sequence = 0

    ch = data.get("channel")
    if ch is None:
        ch = data.get("channel_id")

    return E2AResponse(
        protocol_version=data.get("protocol_version", E2A_PROTOCOL_VERSION),
        response_id=data.get("response_id"),
        request_id=data.get("request_id"),
        sequence=sequence,
        is_final=bool(data.get("is_final", False)),
        status=str(data.get("status", E2A_RESPONSE_STATUS_IN_PROGRESS)),
        response_kind=str(data.get("response_kind") or ""),
        timestamp=_normalize_timestamp_value(data.get("timestamp")),
        provenance=prov,
        body=dict(data.get("body") or {}),
        jsonrpc_id=data.get("jsonrpc_id"),
        correlation_id=data.get("correlation_id"),
        task_id=data.get("task_id"),
        context_id=data.get("context_id"),
        session_id=data.get("session_id"),
        message_id=data.get("message_id"),
        is_stream=bool(data.get("is_stream", False)),
        identity_origin=origin,
        channel=ch,
        user_id=data.get("user_id"),
        agent_ref=data.get("agent_ref"),
        source_agent_id=data.get("source_agent_id"),
        method=data.get("method"),
        projections=dict(data.get("projections") or {}),
        channel_context=dict(data.get("channel_context") or {}),
        metadata=dict(data.get("metadata") or {}),
        a2a_metadata=dict(data.get("a2a_metadata") or {}),
        acp_meta=dict(data.get("acp_meta") or {}),
    )


def merge_params_to_acp_prompt(envelope: E2AEnvelope) -> dict[str, Any]:
    """
    当 ``method == "session/prompt"`` 时，从 ``envelope.params`` 补全 ACP 所需 ``prompt``，返回新参数字典。

    优先级：
    1. 已有 ``params.prompt`` 则不修改。
    2. 否则若有 ``params.content_blocks``（非空 list），用作 ``prompt``。
    3. 否则用 ``params.text``、``params.content``、``params.query`` 中第一个非空字符串生成单条 text ContentBlock。

    随后按需补 ``session_id``、``params._meta``（来自 ``envelope.acp_meta``）。
    """
    p = dict(envelope.params)
    if envelope.method != "session/prompt":
        return p
    if "prompt" in p:
        return p
    blocks: list[dict[str, Any]] = []
    cb = p.get("content_blocks")
    if isinstance(cb, list) and cb:
        blocks.extend(cb)
    else:
        text = p.get("text") or p.get("content") or p.get("query")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
    if blocks:
        p["prompt"] = blocks
    if envelope.session_id and "session_id" not in p:
        p["session_id"] = envelope.session_id
    if envelope.acp_meta:
        p.setdefault("_meta", {}).update(envelope.acp_meta)
    return p
