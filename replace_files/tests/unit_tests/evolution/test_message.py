# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for schema models."""

import pytest

from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.message import ReqMethod, EventType, Mode, Message


class TestReqMethod:
    """Test ReqMethod enum."""

    @staticmethod
    def test_chat_methods():
        """Test chat-related request methods."""
        assert ReqMethod.CHAT_SEND.value == "chat.send"
        assert ReqMethod.CHAT_RESUME.value == "chat.resume"
        assert ReqMethod.CHAT_CANCEL.value == "chat.interrupt"
        assert ReqMethod.CHAT_ANSWER.value == "chat.user_answer"

    @staticmethod
    def test_config_methods():
        """Test config-related request methods."""
        assert ReqMethod.CONFIG_GET.value == "config.get"
        assert ReqMethod.CONFIG_SET.value == "config.set"

    @staticmethod
    def test_session_methods():
        """Test session-related request methods."""
        assert ReqMethod.SESSION_LIST.value == "session.list"
        assert ReqMethod.SESSION_CREATE.value == "session.create"
        assert ReqMethod.SESSION_DELETE.value == "session.delete"

    @staticmethod
    def test_skills_methods():
        """Test skills-related request methods."""
        assert ReqMethod.SKILLS_LIST.value == "skills.list"
        assert ReqMethod.SKILLS_INSTALL.value == "skills.install"
        assert ReqMethod.SKILLS_UNINSTALL.value == "skills.uninstall"


class TestEventType:
    """Test EventType enum."""

    @staticmethod
    def test_connection_events():
        """Test connection-related event types."""
        assert EventType.CONNECTION_ACK.value == "connection.ack"
        assert EventType.HELLO.value == "hello"

    @staticmethod
    def test_chat_events():
        """Test chat-related event types."""
        assert EventType.CHAT_DELTA.value == "chat.delta"
        assert EventType.CHAT_FINAL.value == "chat.final"
        assert EventType.CHAT_TOOL_CALL.value == "chat.tool_call"
        assert EventType.CHAT_TOOL_CALLS_DELTA.value == "chat.tool_calls.delta"
        assert EventType.CHAT_ERROR.value == "chat.error"


class TestMode:
    """Test Mode enum."""

    @staticmethod
    def test_mode_values():
        """Test mode enum values."""
        assert Mode.AGENT_PLAN.value == "agent.plan"
        assert Mode.AGENT_FAST.value == "agent.fast"
        assert Mode.CODE_PLAN.value == "code.plan"
        assert Mode.CODE_NORMAL.value == "code.normal"
        assert Mode.TEAM.value == "team"

    @staticmethod
    def test_mode_from_raw_legacy_compatibility():
        """Test only new mode strings are accepted directly."""
        assert Mode.from_raw("agent.plan") == Mode.AGENT_PLAN
        assert Mode.from_raw("agent.fast") == Mode.AGENT_FAST
        assert Mode.from_raw("code.plan") == Mode.CODE_PLAN
        assert Mode.from_raw("code.normal") == Mode.CODE_NORMAL
        assert Mode.from_raw("team") == Mode.TEAM
        assert Mode.from_raw("invalid") == Mode.AGENT_PLAN

    @staticmethod
    def test_mode_to_runtime_mode():
        """Test runtime mode mapping returns new mode values."""
        assert Mode.AGENT_PLAN.to_runtime_mode() == "agent.plan"
        assert Mode.AGENT_FAST.to_runtime_mode() == "agent.fast"
        assert Mode.CODE_PLAN.to_runtime_mode() == "code.plan"
        assert Mode.CODE_NORMAL.to_runtime_mode() == "code.normal"
        assert Mode.TEAM.to_runtime_mode() == "team"


class TestAgentRequest:
    """Test AgentRequest dataclass."""

    @staticmethod
    def test_create_agent_request_minimal():
        """Test creating AgentRequest with minimal fields."""
        request = AgentRequest(request_id="test-123")
        assert request.request_id == "test-123"
        assert request.channel_id == ""
        assert request.session_id is None
        assert request.req_method is None
        assert request.params == {}
        assert request.is_stream is False

    @staticmethod
    def test_create_agent_request_full():
        """Test creating AgentRequest with all fields."""
        request = AgentRequest(
            request_id="test-456",
            channel_id="web",
            session_id="session-abc",
            req_method=ReqMethod.CHAT_SEND,
            params={"message": "Hello"},
            is_stream=True,
            timestamp=1234567890.0,
            metadata={"user_id": "user1"},
        )
        assert request.request_id == "test-456"
        assert request.channel_id == "web"
        assert request.session_id == "session-abc"
        assert request.req_method == ReqMethod.CHAT_SEND
        assert request.params == {"message": "Hello"}
        assert request.is_stream is True
        assert request.timestamp == 1234567890.0
        assert request.metadata == {"user_id": "user1"}


class TestAgentResponse:
    """Test AgentResponse dataclass."""

    @staticmethod
    def test_create_agent_response_success():
        """Test creating successful AgentResponse."""
        response = AgentResponse(
            request_id="req-1",
            channel_id="web",
            ok=True,
            payload={"result": "success"},
        )
        assert response.request_id == "req-1"
        assert response.channel_id == "web"
        assert response.ok is True
        assert response.payload == {"result": "success"}
        assert response.metadata is None

    @staticmethod
    def test_create_agent_response_error():
        """Test creating error AgentResponse."""
        response = AgentResponse(
            request_id="req-2",
            channel_id="web",
            ok=False,
            payload={"error": "Something went wrong"},
            metadata={"error_code": 500},
        )
        assert response.ok is False
        assert response.payload["error"] == "Something went wrong"
        assert response.metadata["error_code"] == 500


class TestAgentResponseChunk:
    """Test AgentResponseChunk dataclass."""

    @staticmethod
    def test_create_response_chunk():
        """Test creating AgentResponseChunk."""
        chunk = AgentResponseChunk(
            request_id="req-3",
            channel_id="web",
            payload={"delta": "Hello"},
            is_complete=False,
        )
        assert chunk.request_id == "req-3"
        assert chunk.channel_id == "web"
        assert chunk.payload == {"delta": "Hello"}
        assert chunk.is_complete is False

    @staticmethod
    def test_create_final_chunk():
        """Test creating final response chunk."""
        chunk = AgentResponseChunk(
            request_id="req-4",
            channel_id="web",
            is_complete=True,
        )
        assert chunk.is_complete is True
        assert chunk.payload is None


class TestMessage:
    """Test Message dataclass."""

    @staticmethod
    def test_create_request_message():
        """Test creating a request message."""
        message = Message(
            id="msg-1",
            type="req",
            channel_id="web",
            session_id="session-1",
            params={"query": "test"},
            timestamp=1234567890.0,
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
        )
        assert message.id == "msg-1"
        assert message.type == "req"
        assert message.channel_id == "web"
        assert message.session_id == "session-1"
        assert message.params == {"query": "test"}
        assert message.req_method == ReqMethod.CHAT_SEND
        assert message.ok is True
        assert message.payload is None
        assert message.event_type is None

    @staticmethod
    def test_create_response_message():
        """Test creating a response message."""
        message = Message(
            id="msg-2",
            type="res",
            channel_id="web",
            session_id="session-1",
            params={},
            timestamp=1234567891.0,
            ok=True,
            payload={"response": "Hello"},
        )
        assert message.type == "res"
        assert message.payload == {"response": "Hello"}

    @staticmethod
    def test_create_event_message():
        """Test creating an event message."""
        message = Message(
            id="msg-3",
            type="event",
            channel_id="web",
            session_id="session-1",
            params={},
            timestamp=1234567892.0,
            ok=True,
            event_type=EventType.CHAT_DELTA,
        )
        assert message.type == "event"
        assert message.event_type == EventType.CHAT_DELTA

    @staticmethod
    def test_create_streaming_message():
        """Test creating a streaming message."""
        message = Message(
            id="msg-4",
            type="res",
            channel_id="web",
            session_id="session-1",
            params={},
            timestamp=1234567893.0,
            ok=True,
            is_stream=True,
            stream_seq=1,
            stream_id="stream-123",
        )
        assert message.is_stream is True
        assert message.stream_seq == 1
        assert message.stream_id == "stream-123"

    @staticmethod
    def test_message_mode():
        """Test message mode field."""
        plan_message = Message(
            id="msg-5",
            type="req",
            channel_id="web",
            session_id=None,
            params={},
            timestamp=1234567894.0,
            ok=True,
            mode=Mode.AGENT_PLAN,
        )
        assert plan_message.mode == Mode.AGENT_PLAN

        agent_message = Message(
            id="msg-6",
            type="req",
            channel_id="web",
            session_id=None,
            params={},
            timestamp=1234567895.0,
            ok=True,
            mode=Mode.AGENT_FAST,
        )
        assert agent_message.mode == Mode.AGENT_FAST
