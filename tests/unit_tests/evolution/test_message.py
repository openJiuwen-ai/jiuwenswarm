# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for schema models."""

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod, EventType, Mode, Message


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
    def test_personal_context_methods():
        """Test PersonalContext request methods exposed through the WebSocket gateway."""
        methods = {
            item.value
            for item in ReqMethod
            if item.value.startswith("personal_context.")
        }
        assert methods == {
            "personal_context.runtime.status",
            "personal_context.runtime.start_collection",
            "personal_context.runtime.stop_collection",
            "personal_context.runtime.start_agent_use",
            "personal_context.runtime.stop_agent_use",
            "personal_context.runtime.get_config",
            "personal_context.runtime.patch_config",
            "personal_context.runtime.select_model",
            "personal_context.fetch.list_services",
            "personal_context.fetch.create_service",
            "personal_context.fetch.delete_service",
            "personal_context.fetch.patch_service",
            "personal_context.fetch.start_service",
            "personal_context.fetch.stop_run",
            "personal_context.fetch.stop_service",
            "personal_context.fetch.run_all",
            "personal_context.fetch.run_one",
            "personal_context.fetch.get_run_status",
            "personal_context.fetch.get_authorization_status",
            "personal_context.fetch.authorize_provider",
            "personal_context.context.stream_graph",
            "personal_context.context.stream_tree",
            "personal_context.context.search_pages",
            "personal_context.context.get_node",
            "personal_context.context.get_source",
        }
        assert len(methods) == 25

    @staticmethod
    def test_session_methods():
        """Test session-related request methods."""
        assert ReqMethod.SESSION_LIST.value == "session.list"
        assert ReqMethod.SESSION_CREATE.value == "session.create"
        assert ReqMethod.SESSION_DELETE.value == "session.delete"
        assert ReqMethod.TEAM_DELETE.value == "team.delete"

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
        assert EventType.CHAT_SYMPHONY_STATUS.value == "chat.symphony_status"
        assert EventType.CHAT_ERROR.value == "chat.error"


class TestMode:
    """Test Mode enum."""

    @staticmethod
    def test_mode_values():
        """Test mode enum values."""
        assert Mode.AGENT.value == "agent"
        assert Mode.AGENT_PLAN.value == "agent.plan"
        assert Mode.AGENT_FAST.value == "agent.fast"
        assert Mode.CODE_PLAN.value == "code.plan"
        assert Mode.CODE_NORMAL.value == "code.normal"
        assert Mode.CODE_TEAM.value == "code.team"
        assert Mode.TEAM.value == "team"
        assert Mode.TEAM_PLAN_NORMAL.value == "team.plan.normal"
        assert Mode.TEAM_PLAN_CODE.value == "team.plan.code"

    @staticmethod
    def test_mode_from_raw_legacy_compatibility():
        """旧 canonical 通过 DEPRECATION_MAP 静默映射到新 canonical。"""
        assert Mode.from_raw("agent") == Mode.AGENT_WORK_NORMAL
        assert Mode.from_raw("agent.plan") == Mode.AGENT_WORK_PLAN
        assert Mode.from_raw("agent.fast") == Mode.AGENT_WORK_NORMAL
        assert Mode.from_raw("plan") == Mode.AGENT_WORK_NORMAL
        assert Mode.from_raw("fast") == Mode.AGENT_WORK_NORMAL
        # 旧 code / team canonical 同样映射到对应新 canonical。
        assert Mode.from_raw("code.plan") == Mode.AGENT_CODE_PLAN
        assert Mode.from_raw("code.normal") == Mode.AGENT_CODE_NORMAL
        assert Mode.from_raw("code.team") == Mode.TEAM_CODE_NORMAL
        assert Mode.from_raw("team") == Mode.TEAM_WORK_NORMAL
        assert Mode.from_raw("team.plan") == Mode.TEAM_WORK_PLAN
        assert Mode.from_raw("team.plan.normal") == Mode.TEAM_WORK_PLAN
        assert Mode.from_raw("team.plan.code") == Mode.TEAM_CODE_PLAN
        # 新 canonical 原样返回；非法串回落到默认 AGENT_WORK_NORMAL。
        assert Mode.from_raw("agent.work.normal") == Mode.AGENT_WORK_NORMAL
        assert Mode.from_raw("team.code.plan") == Mode.TEAM_CODE_PLAN
        assert Mode.from_raw("invalid") == Mode.AGENT_WORK_NORMAL

    @staticmethod
    def test_mode_to_runtime_mode():
        """Test runtime mode mapping returns canonical mode values."""
        assert Mode.AGENT.to_runtime_mode() == "agent"
        assert Mode.AGENT_PLAN.to_runtime_mode() == "agent"
        assert Mode.AGENT_FAST.to_runtime_mode() == "agent"
        assert Mode.CODE_PLAN.to_runtime_mode() == "code.plan"
        assert Mode.CODE_NORMAL.to_runtime_mode() == "code.normal"
        assert Mode.CODE_TEAM.to_runtime_mode() == "code.team"
        assert Mode.TEAM.to_runtime_mode() == "team"
        assert Mode.TEAM_PLAN_NORMAL.to_runtime_mode() == "team.plan.normal"
        assert Mode.TEAM_PLAN_CODE.to_runtime_mode() == "team.plan.code"
        # 新三段命名 canonical：原样返回，下游 acp_connect.py 注入 params["mode"]。
        assert Mode.AGENT_WORK_NORMAL.to_runtime_mode() == "agent.work.normal"
        assert Mode.AGENT_WORK_PLAN.to_runtime_mode() == "agent.work.plan"
        assert Mode.AGENT_CODE_NORMAL.to_runtime_mode() == "agent.code.normal"
        assert Mode.AGENT_CODE_PLAN.to_runtime_mode() == "agent.code.plan"
        assert Mode.TEAM_WORK_NORMAL.to_runtime_mode() == "team.work.normal"
        assert Mode.TEAM_WORK_PLAN.to_runtime_mode() == "team.work.plan"
        assert Mode.TEAM_CODE_NORMAL.to_runtime_mode() == "team.code.normal"
        assert Mode.TEAM_CODE_PLAN.to_runtime_mode() == "team.code.plan"


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
    def test_message_mode_default_aligns_with_from_raw_fallback():
        """客户端不传 mode 时 Message.mode 与 Mode.from_raw 的 fallback 对齐。

        钉死 schema 字段默认值 = ``Mode.AGENT_WORK_NORMAL``，与
        ``Mode.from_raw(None)`` / ``Mode.from_raw("")`` 的 fallback 保持一致。
        旧契约是字段默认 ``Mode.AGENT``（值 ``"agent"``），P2 迁移把
        from_raw 的 fallback 改为 ``agent.work.normal`` 后两处不一致，此处
        钉死新契约，防止回归。下游若要做字面量比较应使用谓词
        （``is_work_mode`` / ``to_runtime_mode()``），而非 ``mode.value == "agent"``。
        """
        # 构造 Message 不传 mode 字段
        message = Message(
            id="msg-default-mode",
            type="req",
            channel_id="web",
            session_id=None,
            params={},
            timestamp=1234567894.0,
            ok=True,
        )
        # 字段默认值钉死
        assert message.mode is Mode.AGENT_WORK_NORMAL
        assert message.mode.value == "agent.work.normal"
        # 与 from_raw 的 fallback 一致（None / 空串 / 非法串三条入口都同值）
        assert Mode.from_raw(None) == message.mode
        assert Mode.from_raw("") == message.mode
        assert Mode.from_raw("invalid") == message.mode

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
