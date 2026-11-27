# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer ↔ Gateway WebSocket：E2AResponse 线编码 / 解码与 legacy 兜底。"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from jiuwenclaw.e2a.constants import (
    E2A_RESPONSE_KIND_E2A_ERROR,
    E2A_RESPONSE_STATUS_FAILED,
    E2A_WIRE_LEGACY_AGENT_CHUNK_KEY,
    E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY,
)
from jiuwenclaw.e2a.gateway_normalize import (
    e2a_response_from_agent_chunk,
    e2a_response_from_agent_response,
    e2a_response_to_agent_chunk,
    e2a_response_to_agent_response,
)
from jiuwenclaw.e2a.constants import E2A_SOURCE_PROTOCOL_E2A
from jiuwenclaw.e2a.models import (
    E2A_PROTOCOL_VERSION,
    E2AProvenance,
    E2AResponse,
    IdentityOrigin,
    utc_now_iso,
)
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk

logger = logging.getLogger(__name__)


def _raw_dict_to_agent_response(data: dict[str, Any]) -> AgentResponse:
    return AgentResponse(
        request_id=str(data["request_id"]),
        channel_id=str(data.get("channel_id", "")),
        ok=bool(data.get("ok", True)),
        payload=data.get("payload"),
        metadata=data.get("metadata"),
    )


def _raw_dict_to_agent_chunk(data: dict[str, Any]) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=str(data["request_id"]),
        channel_id=str(data.get("channel_id", "")),
        payload=data.get("payload"),
        is_complete=bool(data.get("is_complete", False)),
    )


def is_e2a_response_wire_dict(data: dict[str, Any]) -> bool:
    """判别 JSON 对象是否为 E2A 响应线格式（与 ``E2AEnvelope`` 区分：须含非空 ``response_kind``）。"""
    if not isinstance(data, dict) or data.get("type") == "event":
        return False
    if data.get("protocol_version") != E2A_PROTOCOL_VERSION:
        return False
    rk = data.get("response_kind")
    return isinstance(rk, str) and bool(rk.strip())


def _deprecated_unary_shape(data: dict[str, Any]) -> bool:
    return (
        isinstance(data, dict)
        and "request_id" in data
        and "channel_id" in data
        and "ok" in data
        and not is_e2a_response_wire_dict(data)
    )


def _deprecated_chunk_shape(data: dict[str, Any]) -> bool:
    return (
        isinstance(data, dict)
        and "request_id" in data
        and "channel_id" in data
        and "is_complete" in data
        and "payload" in data
        and "ok" not in data
        and not is_e2a_response_wire_dict(data)
    )


def parse_agent_server_wire_unary(data: dict[str, Any]) -> AgentResponse:
    """将一条非流式 WebSocket JSON 解析为 ``AgentResponse``。"""
    rid = str(data.get("request_id", ""))
    if is_e2a_response_wire_dict(data):
        try:
            e2a = E2AResponse.from_dict(dict(data))
        except Exception as e:
            logger.exception(
                "[E2A][wire][in][FAIL] stage=from_dict unary request_id=%s err=%s",
                rid,
                e,
            )
            raise
        meta = dict(e2a.metadata or {})
        legacy = meta.get(E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY)
        if legacy is not None and isinstance(legacy, dict):
            logger.warning(
                "[E2A][wire][in][fallback] unary request_id=%s response_id=%s legacy_key=%s json_bytes≈%s",
                rid,
                e2a.response_id,
                E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY,
                len(str(legacy).encode("utf-8", errors="replace")),
            )
            return _raw_dict_to_agent_response(legacy)
        try:
            out = e2a_response_to_agent_response(e2a)
            logger.debug(
                "[E2A][wire][in] unary request_id=%s response_kind=%s",
                rid,
                e2a.response_kind,
            )
            return out
        except Exception as e:
            logger.exception(
                "[E2A][wire][in][FAIL] stage=inverse unary request_id=%s response_kind=%s err=%s",
                rid,
                e2a.response_kind,
                e,
            )
            legacy_inv = meta.get(E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY)
            if isinstance(legacy_inv, dict):
                logger.warning(
                    "[E2A][wire][in][fallback] unary inverse failed, using legacy blob request_id=%s",
                    rid,
                )
                return _raw_dict_to_agent_response(legacy_inv)
            raise

    if _deprecated_unary_shape(data):
        logger.warning(
            "[E2A][wire][in][deprecated_legacy_shape] unary request_id=%s keys=%s",
            rid,
            list(data.keys())[:24],
        )
        return _raw_dict_to_agent_response(data)

    raise ValueError(f"parse_agent_server_wire_unary: unrecognized wire shape keys={list(data.keys())[:32]}")


def parse_agent_server_wire_chunk(data: dict[str, Any]) -> AgentResponseChunk:
    """将一条流式 WebSocket JSON 解析为 ``AgentResponseChunk``。"""
    rid = str(data.get("request_id", ""))
    if is_e2a_response_wire_dict(data):
        try:
            e2a = E2AResponse.from_dict(dict(data))
        except Exception as e:
            logger.exception(
                "[E2A][wire][in][FAIL] stage=from_dict chunk request_id=%s err=%s",
                rid,
                e,
            )
            raise
        meta = dict(e2a.metadata or {})
        legacy = meta.get(E2A_WIRE_LEGACY_AGENT_CHUNK_KEY)
        if legacy is not None and isinstance(legacy, dict):
            logger.warning(
                "[E2A][wire][in][fallback] chunk request_id=%s response_id=%s legacy_key=%s json_bytes≈%s",
                rid,
                e2a.response_id,
                E2A_WIRE_LEGACY_AGENT_CHUNK_KEY,
                len(str(legacy).encode("utf-8", errors="replace")),
            )
            return _raw_dict_to_agent_chunk(legacy)
        try:
            out = e2a_response_to_agent_chunk(e2a)
            logger.debug(
                "[E2A][wire][in] chunk request_id=%s response_kind=%s is_final=%s",
                rid,
                e2a.response_kind,
                e2a.is_final,
            )
            return out
        except Exception as e:
            logger.exception(
                "[E2A][wire][in][FAIL] stage=inverse chunk request_id=%s response_kind=%s is_final=%s err=%s",
                rid,
                e2a.response_kind,
                e2a.is_final,
                e,
            )
            legacy_inv = meta.get(E2A_WIRE_LEGACY_AGENT_CHUNK_KEY)
            if isinstance(legacy_inv, dict):
                logger.warning(
                    "[E2A][wire][in][fallback] chunk inverse failed, using legacy blob request_id=%s",
                    rid,
                )
                return _raw_dict_to_agent_chunk(legacy_inv)
            raise

    if _deprecated_chunk_shape(data):
        logger.warning(
            "[E2A][wire][in][deprecated_legacy_shape] chunk request_id=%s keys=%s",
            rid,
            list(data.keys())[:24],
        )
        return _raw_dict_to_agent_chunk(data)

    raise ValueError(f"parse_agent_server_wire_chunk: unrecognized wire shape keys={list(data.keys())[:32]}")


def encode_agent_response_for_wire(
    resp: AgentResponse,
    *,
    response_id: str,
    sequence: int = 0,
) -> dict[str, Any]:
    """``AgentResponse`` → E2A 线 dict；失败时 ``metadata`` 塞入整包 legacy 并记日志。"""
    rid = resp.request_id
    try:
        e2a = e2a_response_from_agent_response(
            resp, response_id=response_id, sequence=sequence
        )
        try:
            wire = e2a.to_dict()
        except Exception as te:
            logger.exception(
                "[E2A][wire][out][FAIL] stage=to_dict unary request_id=%s response_id=%s err=%s legacy_stashed=true",
                rid,
                response_id,
                te,
            )
            return _fallback_wire_unary_from_legacy(
                asdict(resp), response_id=response_id, sequence=sequence, exc=te
            )
        logger.info(
            "[E2A][wire][out] unary request_id=%s response_id=%s response_kind=%s legacy_stashed=false",
            rid,
            response_id,
            e2a.response_kind,
        )
        return wire
    except Exception as e:
        logger.exception(
            "[E2A][wire][out][FAIL] stage=encode unary request_id=%s response_id=%s err=%s legacy_stashed=true",
            rid,
            response_id,
            e,
        )
        return _fallback_wire_unary_from_legacy(
            asdict(resp), response_id=response_id, sequence=sequence, exc=e
        )


def encode_agent_chunk_for_wire(
    chunk: AgentResponseChunk,
    *,
    response_id: str,
    sequence: int,
    is_stream: bool = True,
) -> dict[str, Any]:
    """``AgentResponseChunk`` → E2A 线 dict；失败时 ``metadata`` 塞入整包 legacy。"""
    rid = chunk.request_id
    try:
        e2a = e2a_response_from_agent_chunk(
            chunk,
            response_id=response_id,
            sequence=sequence,
            is_stream=is_stream,
        )
        try:
            wire = e2a.to_dict()
        except Exception as te:
            logger.exception(
                (
                    "[E2A][wire][out][FAIL] stage=to_dict chunk request_id=%s response_id=%s "
                    "seq=%s err=%s legacy_stashed=true"
                ),
                rid,
                response_id,
                sequence,
                te,
            )
            return _fallback_wire_chunk_from_legacy(
                asdict(chunk),
                response_id=response_id,
                sequence=sequence,
                exc=te,
                is_stream=is_stream,
            )
        logger.info(
            (
                "[E2A][wire][out] chunk request_id=%s response_id=%s seq=%s response_kind=%s "
                "is_final=%s legacy_stashed=false"
            ),
            rid,
            response_id,
            sequence,
            e2a.response_kind,
            e2a.is_final,
        )
        return wire
    except Exception as e:
        logger.exception(
            "[E2A][wire][out][FAIL] stage=encode chunk request_id=%s response_id=%s seq=%s err=%s legacy_stashed=true",
            rid,
            response_id,
            sequence,
            e,
        )
        return _fallback_wire_chunk_from_legacy(
            asdict(chunk),
            response_id=response_id,
            sequence=sequence,
            exc=e,
            is_stream=is_stream,
        )


def _fallback_wire_unary_from_legacy(
    legacy: dict[str, Any],
    *,
    response_id: str,
    sequence: int,
    exc: BaseException,
) -> dict[str, Any]:
    ts = utc_now_iso()
    prov = E2AProvenance(
        source_protocol=E2A_SOURCE_PROTOCOL_E2A,
        converter="jiuwenclaw.e2a.wire_codec:_fallback_wire_unary_from_legacy",
        converted_at=ts,
        details={"error": str(exc), "kind": "wire_encode_fallback"},
    )
    e2a = E2AResponse(
        protocol_version=E2A_PROTOCOL_VERSION,
        response_id=response_id,
        request_id=str(legacy.get("request_id", "")),
        sequence=sequence,
        is_final=True,
        status=E2A_RESPONSE_STATUS_FAILED,
        response_kind=E2A_RESPONSE_KIND_E2A_ERROR,
        timestamp=ts,
        provenance=prov,
        body={
            "code": "E2A.WIRE_ENCODE_ERROR",
            "message": "Failed to encode AgentResponse as E2A; see metadata legacy blob",
            "details": {"error": str(exc)},
        },
        channel=str(legacy.get("channel_id") or "") or None,
        metadata={E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY: legacy},
        identity_origin=IdentityOrigin.AGENT,
        is_stream=False,
    )
    return e2a.to_dict()


def _fallback_wire_chunk_from_legacy(
    legacy: dict[str, Any],
    *,
    response_id: str,
    sequence: int,
    exc: BaseException,
    is_stream: bool,
) -> dict[str, Any]:
    ts = utc_now_iso()
    prov = E2AProvenance(
        source_protocol=E2A_SOURCE_PROTOCOL_E2A,
        converter="jiuwenclaw.e2a.wire_codec:_fallback_wire_chunk_from_legacy",
        converted_at=ts,
        details={"error": str(exc), "kind": "wire_encode_chunk_fallback"},
    )
    e2a = E2AResponse(
        protocol_version=E2A_PROTOCOL_VERSION,
        response_id=response_id,
        request_id=str(legacy.get("request_id", "")),
        sequence=sequence,
        is_final=bool(legacy.get("is_complete", False)),
        status=E2A_RESPONSE_STATUS_FAILED,
        response_kind=E2A_RESPONSE_KIND_E2A_ERROR,
        timestamp=ts,
        provenance=prov,
        body={
            "code": "E2A.WIRE_ENCODE_ERROR",
            "message": "Failed to encode AgentResponseChunk as E2A; see metadata legacy blob",
            "details": {"error": str(exc)},
        },
        channel=str(legacy.get("channel_id") or "") or None,
        metadata={E2A_WIRE_LEGACY_AGENT_CHUNK_KEY: legacy},
        identity_origin=IdentityOrigin.AGENT,
        is_stream=is_stream,
    )
    return e2a.to_dict()


def encode_json_parse_error_wire(
    *,
    request_id: str,
    channel_id: str,
    message: str,
    response_id: str = "",
) -> dict[str, Any]:
    """入站 JSON 无法解析时发送的单帧 E2A 形错误（无 legacy blob）。"""
    ts = utc_now_iso()
    rid_out = response_id or (request_id or "invalid-json")
    e2a = E2AResponse(
        protocol_version=E2A_PROTOCOL_VERSION,
        response_id=rid_out,
        request_id=request_id or None,
        sequence=0,
        is_final=True,
        status=E2A_RESPONSE_STATUS_FAILED,
        response_kind=E2A_RESPONSE_KIND_E2A_ERROR,
        timestamp=ts,
        provenance=E2AProvenance(
            source_protocol=E2A_SOURCE_PROTOCOL_E2A,
            converter="jiuwenclaw.e2a.wire_codec:encode_json_parse_error_wire",
            converted_at=ts,
            details={"kind": "json_parse_error"},
        ),
        body={
            "code": "E2A.INVALID_JSON",
            "message": message,
            "details": {},
        },
        channel=channel_id or None,
        identity_origin=IdentityOrigin.AGENT,
        is_stream=False,
    )
    return e2a.to_dict()
