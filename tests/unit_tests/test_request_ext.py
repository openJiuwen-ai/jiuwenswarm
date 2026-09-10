# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.common.request_ext import (
    INTERNAL_HEADER_NAME,
    MAX_INTERNAL_JSON_BYTES,
    METADATA_KEY,
    RequestExtCodecError,
    attach_to_metadata,
    build_ext_from_source,
    decode_internal_header,
    encode_internal_header,
    get_ext,
    lift_from_metadata,
    reset_ext,
    set_current,
    set_forward_headers,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


@pytest.fixture(autouse=True)
def _reset_forward_headers():
    set_forward_headers(None)
    yield
    set_forward_headers(None)


def test_build_ext_from_query_lists(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS", "user_id,group_id,bot_id")
    ext = build_ext_from_source({
        "user_id": ["u1"],
        "group_id": ["g1"],
        "other": ["x"],
    })
    assert ext == {"user_id": "u1", "group_id": "g1"}


def test_build_ext_legacy_env_name(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS", raising=False)
    monkeypatch.setenv("JIUWENCLAW_REQUEST_EXT_FORWARD_HEADERS", "bot_id")
    ext = build_ext_from_source({"bot_id": "b1"})
    assert ext == {"bot_id": "b1"}


def test_build_ext_never_accepts_reserved_internal_header():
    set_forward_headers([INTERNAL_HEADER_NAME, "tenant"])
    ext = build_ext_from_source({INTERNAL_HEADER_NAME: "forged", "tenant": "t1"})
    assert ext == {"tenant": "t1"}


def test_attach_and_lift_roundtrip():
    meta = attach_to_metadata({"method": "chat.send"}, ext={"user_id": "u1"})
    assert meta[METADATA_KEY] == {"user_id": "u1"}
    token = lift_from_metadata(meta)
    try:
        assert get_ext() == {"user_id": "u1"}
    finally:
        reset_ext(token)
    assert get_ext() == {}


def test_set_current_context():
    token = set_current({"group_id": "g"})
    try:
        meta = attach_to_metadata({"method": "x"})
        assert meta[METADATA_KEY] == {"group_id": "g"}
    finally:
        reset_ext(token)


def test_internal_header_roundtrip_preserves_json_values():
    ext = {
        "tenant": "华为-tenant-a",
        "custom": {"enabled": True, "levels": [1, 2, 3]},
    }
    encoded = encode_internal_header(ext)
    assert encoded is not None
    assert all(ch.isalnum() or ch in "-_" for ch in encoded)
    assert decode_internal_header(encoded) == ext


@pytest.mark.parametrize("value", ["not+base64", "e30=", "WzEsMl0", "e25vdC1qc29ufQ"])
def test_internal_header_rejects_invalid_payload(value):
    with pytest.raises(RequestExtCodecError):
        decode_internal_header(value)


def test_internal_header_rejects_non_json_value():
    with pytest.raises(RequestExtCodecError, match="JSON values"):
        encode_internal_header({"bad": object()})


def test_internal_header_rejects_oversized_json():
    with pytest.raises(RequestExtCodecError, match="exceeds"):
        encode_internal_header({"large": "x" * MAX_INTERNAL_JSON_BYTES})


@pytest.mark.asyncio
async def test_agent_pipeline_exposes_ext_to_rail_and_resets_after_request(monkeypatch):
    """AgentServer 流水线内 Rail 可读，请求结束后不会串到下一请求。"""
    from jiuwenswarm.server import pipeline

    observed: list[dict] = []

    async def _capture_ext(_ctx, _request):
        observed.append(get_ext())
        return True

    monkeypatch.setattr(pipeline, "dispatch_with_context", _capture_ext)
    request = AgentRequest(
        request_id="rail-ext-1",
        channel_id="web",
        req_method=ReqMethod.SESSION_LIST,
        metadata={METADATA_KEY: {"tenant": "租户-a", "trace": "trace-1"}},
    )

    await pipeline.dispatch_parsed_request(object(), request)

    assert observed == [{"tenant": "租户-a", "trace": "trace-1"}]
    assert get_ext() == {}
