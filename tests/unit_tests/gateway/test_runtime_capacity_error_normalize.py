"""资源超限（max_services / 100001）应下发 chat.error，而非空 chat.final。"""

from __future__ import annotations

import os

import pytest

from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


@pytest.fixture(autouse=True)
def _enable_agent_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")


def test_normalize_capacity_100001_to_chat_error():
    out = MessageHandler._normalize_runtime_failure_payload(
        {"error_code": 100001, "message": "服务并发度超过上限，消息请求失败"}
    )
    assert out is not None
    assert out["event_type"] == "chat.error"
    assert out["error"] == (
        "Request exceeds the maximum connection limit. Please try again later."
    )
    assert out["code"] == "100001"
    assert out["error_code"] == 100001
    assert out["message"] == "服务并发度超过上限，消息请求失败"


def test_normalize_capacity_100002():
    out = MessageHandler._normalize_runtime_failure_payload(
        {"error_code": 100002, "message": "服务启动失败"}
    )
    assert out is not None
    assert out["event_type"] == "chat.error"
    assert out["error"] == "Service failed to start. Please try again later."
    assert out["code"] == "100002"


def test_normalize_ignores_plain_message_without_code():
    assert MessageHandler._normalize_runtime_failure_payload({"message": "普通文本"}) is None


def test_normalize_existing_error_field():
    out = MessageHandler._normalize_runtime_failure_payload({"error": "模型未配置"})
    assert out is not None
    assert out["event_type"] == "chat.error"
    assert out["error"] == "模型未配置"


def test_chunk_to_message_emits_chat_error_for_capacity():
    chunk = AgentResponseChunk(
        request_id="chat-1",
        channel_id="web",
        payload={"error_code": 100001, "message": "服务并发度超过上限，消息请求失败"},
        is_complete=True,
    )
    msg = MessageHandler._chunk_to_message(chunk, session_id="sess_1")
    assert msg.event_type == EventType.CHAT_ERROR
    assert msg.ok is False
    assert msg.payload["error"] == (
        "Request exceeds the maximum connection limit. Please try again later."
    )
    assert msg.payload["code"] == "100001"


def test_response_to_message_emits_chat_error_for_capacity():
    resp = AgentResponse(
        request_id="req-1",
        channel_id="web",
        ok=True,
        payload={"error_code": 100001, "message": "服务并发度超过上限，消息请求失败"},
    )
    msg = MessageHandler._response_to_message(resp, session_id="sess_1")
    assert msg.type == "event"
    assert msg.event_type == EventType.CHAT_ERROR
    assert msg.ok is False
    assert msg.payload["error"] == (
        "Request exceeds the maximum connection limit. Please try again later."
    )


def test_capacity_chunk_is_not_terminal_sentinel():
    chunk = AgentResponseChunk(
        request_id="chat-1",
        channel_id="web",
        payload={"error_code": 100001, "message": "服务并发度超过上限，消息请求失败"},
        is_complete=True,
    )
    assert MessageHandler._is_terminal_stream_chunk(chunk) is False


def test_pure_is_complete_sentinel_still_terminal():
    chunk = AgentResponseChunk(
        request_id="chat-1",
        channel_id="web",
        payload={"is_complete": True},
        is_complete=True,
    )
    assert MessageHandler._is_terminal_stream_chunk(chunk) is True


def test_normalize_skipped_without_agent_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    chunk = AgentResponseChunk(
        request_id="chat-1",
        channel_id="web",
        payload={"error_code": 100001, "message": "服务并发度超过上限，消息请求失败"},
        is_complete=True,
    )
    msg = MessageHandler._chunk_to_message(chunk, session_id="sess_1")
    assert msg.event_type is None
    assert msg.payload.get("error_code") == 100001
