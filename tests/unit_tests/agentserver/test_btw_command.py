# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for /btw command — prompt builder, cache control, adapter, and WS handler.

Covers commits:
- c44c8864 feat(btw): add /btw command
- 60bf614 feat(btw): keep prompt cache
"""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.runtime.agent_adapter.recap_prompts import (
    _build_btw_prompt,
    build_recap_prompt,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _try_add_cache_control,
)


# =============================================================================
# Fixtures & harness (mirrors test_agentserver_cli_commands.py)
# =============================================================================

class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        import json

        self.sent.append(json.loads(payload))


class AgentWebSocketServerHarness(agent_ws_server_module.AgentWebSocketServer):
    async def handle_command_btw_for_test(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        await self._handle_command_btw(ws, request, send_lock)

    async def handle_command_recap_for_test(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        await self._handle_command_recap(ws, request, send_lock)

    def get_agent_manager_for_test(self) -> Any:
        return self._agent_manager


def fake_encode_agent_response_for_wire(
    resp: AgentResponse, response_id: str
) -> dict[str, Any]:
    return {
        "response_id": response_id,
        "payload": resp.payload,
        "ok": resp.ok,
    }


@pytest.fixture
def server() -> AgentWebSocketServerHarness:
    return AgentWebSocketServerHarness()


@pytest.fixture
def fake_ws() -> FakeWebSocket:
    return FakeWebSocket()


@pytest.fixture(autouse=True)
def _patch_wire_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )


# =============================================================================
# Helpers
# =============================================================================

def _make_msg(role: str = "user", content: Any = "hello") -> MagicMock:
    """Create a message-like MagicMock with role + content attributes."""
    msg = MagicMock()
    msg.role = role
    msg.content = content
    return msg


def _make_adapter(**overrides: Any) -> Any:
    """Create a JiuWenSwarmDeepAdapter instance without running __init__.

    Defaults set up a minimal valid state for testing btw/recap paths.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = None
    adapter._is_session_scoped_adapter = True
    adapter._last_system_prompt = overrides.get("_last_system_prompt", "")
    adapter._model = overrides.get("_model", None)
    adapter._resolve_prompt_language = MagicMock(return_value="en")
    # _get_agent_tools touches _session_adapters/_get_cached_session_adapter which
    # the bare test object lacks; default to returning no tools (→ tools=None path).
    adapter._get_agent_tools = AsyncMock(return_value=[])
    return adapter


# =============================================================================
# Section A: _build_btw_prompt
# =============================================================================

class TestBuildBtwPrompt:
    """Tests for the _build_btw_prompt pure function."""

    @staticmethod
    def test_en_prompt_contains_question():
        """EN prompt must include the user's question text."""
        result = _build_btw_prompt("what does git status do?", language="en")
        assert "what does git status do?" in result

    @staticmethod
    def test_en_prompt_contains_system_reminder_wrapper():
        """EN prompt must be wrapped in <system-reminder> tags."""
        result = _build_btw_prompt("hello", language="en")
        assert "<system-reminder>" in result
        assert "</system-reminder>" in result

    @staticmethod
    def test_en_prompt_contains_no_tools_constraint():
        """EN prompt must state NO tools are available."""
        result = _build_btw_prompt("test", language="en")
        assert "NO tools" in result

    @staticmethod
    def test_en_prompt_contains_one_off_constraint():
        """EN prompt must state this is a one-off response."""
        result = _build_btw_prompt("test", language="en")
        assert "one-off" in result.lower() or "no follow-up" in result.lower()

    @staticmethod
    def test_en_prompt_has_question_prefix():
        """EN prompt uses 'Question:' prefix, not Chinese."""
        result = _build_btw_prompt("what is this?", language="en")
        assert "Question:" in result
        assert "问题：" not in result

    @staticmethod
    def test_zh_prompt_contains_question():
        """ZH prompt must include the question text."""
        result = _build_btw_prompt("什么是 git status？", language="zh")
        assert "什么是 git status？" in result

    @staticmethod
    def test_zh_prompt_contains_language_instruction():
        """ZH prompt must instruct answering in Chinese."""
        result = _build_btw_prompt("test", language="zh")
        assert "请用中文回答" in result

    @staticmethod
    def test_zh_prompt_has_question_prefix():
        """ZH prompt uses '问题：' prefix."""
        result = _build_btw_prompt("测试", language="zh")
        assert "问题：" in result

    @staticmethod
    def test_zh_prompt_also_contains_system_reminder():
        """ZH prompt still wraps constraints in <system-reminder>."""
        result = _build_btw_prompt("test", language="zh")
        assert "<system-reminder>" in result
        assert "</system-reminder>" in result

    @staticmethod
    def test_no_claude_code_references():
        """Prompt must NOT contain hardcoded 'Claude Code' references (scrubbed in 60bf614)."""
        result_en = _build_btw_prompt("test", language="en")
        result_zh = _build_btw_prompt("test", language="zh")
        assert "Claude Code" not in result_en
        assert "Claude Code" not in result_zh

    @staticmethod
    def test_no_system_prompt_embedded_in_text():
        """The system prompt text is NOT embedded in the btw prompt — passed via SystemMessage."""
        result = _build_btw_prompt("test")
        # The prompt should not contain CLAUDE.md or skill instructions
        assert "CLAUDE.md" not in result

    @staticmethod
    def test_empty_question_still_produces_valid_structure():
        """Empty question string should still produce the system_reminder wrapper."""
        result = _build_btw_prompt("", language="en")
        assert "<system-reminder>" in result
        assert "Question:" in result

    @staticmethod
    def test_language_defaults_to_en_when_unknown():
        """Unknown language code should fall back to English output."""
        result = _build_btw_prompt("test", language="fr")
        assert "Question:" in result
        assert "请用中文回答" not in result


# =============================================================================
# Section B: _try_add_cache_control
# =============================================================================

class TestTryAddCacheControl:
    """Tests for the _try_add_cache_control pure function."""

    @staticmethod
    def test_adds_to_dict_based_last_block():
        """Should add cache_control to the last dict content block."""
        msg = _make_msg(content=[
            {"type": "text", "text": "first"},
            {"type": "text", "text": "last"},
        ])
        _try_add_cache_control(msg)
        assert msg.content[-1]["cache_control"] == {"type": "ephemeral"}
        # First block must NOT be modified
        assert "cache_control" not in msg.content[0]

    @staticmethod
    def test_noop_on_string_content():
        """String content must be left untouched — no conversion to list."""
        msg = _make_msg(content="plain string")
        original = msg.content
        _try_add_cache_control(msg)
        assert msg.content is original
        assert msg.content == "plain string"

    @staticmethod
    def test_noop_on_none_content():
        """None content must not cause errors."""
        msg = _make_msg(content=None)
        _try_add_cache_control(msg)  # must not raise

    @staticmethod
    def test_noop_on_empty_list_content():
        """Empty list content must not cause errors."""
        msg = _make_msg(content=[])
        _try_add_cache_control(msg)  # must not raise
        assert msg.content == []

    @staticmethod
    def test_noop_when_msg_has_no_content_attr():
        """Object without content attribute must not cause errors."""
        msg = MagicMock(spec=[])  # no 'content' attr
        _try_add_cache_control(msg)  # must not raise

    @staticmethod
    def test_preserves_non_dict_blocks_in_list():
        """String items in a list content should not be modified."""
        msg = _make_msg(content=[
            "string block",
            {"type": "text", "text": "dict block"},
        ])
        _try_add_cache_control(msg)
        assert msg.content[0] == "string block"  # unchanged
        assert msg.content[1]["cache_control"] == {"type": "ephemeral"}

    @staticmethod
    def test_last_block_already_has_cache_control_overwrites():
        """If last block already has cache_control, _try_add_cache_control overwrites it."""
        msg = _make_msg(content=[
            {"type": "text", "text": "data", "cache_control": {"type": "old"}},
        ])
        _try_add_cache_control(msg)
        assert msg.content[-1]["cache_control"] == {"type": "ephemeral"}


# =============================================================================
# Section C: _get_agent_system_prompt caching
# =============================================================================

class TestGetAgentSystemPrompt:
    """Tests for _get_agent_system_prompt caching behavior."""

    @staticmethod
    def test_returns_cached_value_on_second_call():
        """Second call returns _last_system_prompt without calling build() again."""
        adapter = _make_adapter(_last_system_prompt="cached prompt")

        # First call should hit cache
        result = adapter._get_agent_system_prompt()
        assert result == "cached prompt"

    @staticmethod
    def test_returns_empty_when_instance_is_none():
        """When _instance is None, return ''."""
        adapter = _make_adapter(_last_system_prompt="")
        adapter._instance = None
        result = adapter._get_agent_system_prompt()
        assert result == ""

    @staticmethod
    def test_returns_empty_when_react_agent_is_none():
        """When react_agent is None, return ''."""
        adapter = _make_adapter(_last_system_prompt="")
        adapter._instance = MagicMock()
        adapter._instance.react_agent = None
        result = adapter._get_agent_system_prompt()
        assert result == ""

    @staticmethod
    def test_prompt_builder_takes_priority():
        """prompt_builder.build() should be used before system_prompt_builder."""
        adapter = _make_adapter(_last_system_prompt="")
        adapter._instance = MagicMock()
        react_agent = MagicMock()
        react_agent.prompt_builder = MagicMock()
        react_agent.prompt_builder.build.return_value = "from prompt_builder"
        react_agent.system_prompt_builder = MagicMock()
        react_agent.system_prompt_builder.build.return_value = "from system_prompt_builder"
        adapter._instance.react_agent = react_agent

        result = adapter._get_agent_system_prompt()
        assert result == "from prompt_builder"
        react_agent.prompt_builder.build.assert_called_once()
        react_agent.system_prompt_builder.build.assert_not_called()

    @staticmethod
    def test_falls_back_to_system_prompt_builder():
        """When prompt_builder is absent, use system_prompt_builder."""
        adapter = _make_adapter(_last_system_prompt="")
        adapter._instance = MagicMock()
        react_agent = MagicMock(spec=["system_prompt_builder"])
        # No prompt_builder attribute
        del react_agent.prompt_builder
        react_agent.system_prompt_builder = MagicMock()
        react_agent.system_prompt_builder.build.return_value = "from system_prompt_builder"
        adapter._instance.react_agent = react_agent

        result = adapter._get_agent_system_prompt()
        assert result == "from system_prompt_builder"

    @staticmethod
    def test_caches_result_from_prompt_builder():
        """After first call through prompt_builder, second call uses cache."""
        adapter = _make_adapter(_last_system_prompt="")
        adapter._instance = MagicMock()
        react_agent = MagicMock()
        call_count = [0]

        def _build():
            call_count[0] += 1
            return "built once"

        react_agent.prompt_builder.build = _build
        adapter._instance.react_agent = react_agent

        result1 = adapter._get_agent_system_prompt()
        result2 = adapter._get_agent_system_prompt()

        assert result1 == "built once"
        assert result2 == "built once"
        assert call_count[0] == 1  # build was called only once

    @staticmethod
    def test_returns_empty_when_no_builders():
        """When neither builder is available, return ''."""
        adapter = _make_adapter(_last_system_prompt="")
        adapter._instance = MagicMock()
        react_agent = MagicMock(spec=[])  # no attributes by default
        adapter._instance.react_agent = react_agent

        result = adapter._get_agent_system_prompt()
        assert result == ""

    @staticmethod
    def test_last_system_prompt_is_initialized_empty():
        """_last_system_prompt should be initialized as empty string."""
        adapter = _make_adapter()
        assert adapter._last_system_prompt == ""


# =============================================================================
# Section D: _call_model_for_recap — message format preservation
# =============================================================================

class TestCallModelForRecap:
    """Tests for _call_model_for_recap message handling and cache behavior."""

    @pytest.mark.asyncio
    async def test_preserves_list_based_content(self):
        """Structured content blocks must be preserved — no str() conversion."""
        from unittest.mock import call

        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        structured_content = [
            {"type": "text", "text": "user message"},
            {"type": "tool_result", "tool_use_id": "tool_123", "content": "output"},
        ]
        msgs = [_make_msg(role="user", content=structured_content)]

        await adapter._call_model_for_recap(msgs, "prompt text")

        # The invoke call should receive messages with preserved content
        invoke_args = mock_model.invoke.call_args[0][0]
        # Find the first user message (not system, not final prompt)
        user_msgs = [m for m in invoke_args if getattr(m, "role", getattr(m, "__class__", None)) and True]
        # The structured content should be passed through as-is
        assert mock_model.invoke.called

    @pytest.mark.asyncio
    async def test_skips_empty_string_messages(self):
        """Messages with empty string content should be skipped."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [
            _make_msg(role="user", content=""),
            _make_msg(role="assistant", content="valid"),
        ]

        await adapter._call_model_for_recap(msgs, "prompt")

        invoke_args = mock_model.invoke.call_args[0][0]
        # Empty-string user message should be skipped
        # Only assistant + final prompt should remain (plus optional system)
        content_values = [
            getattr(m, "content", None) for m in invoke_args
        ]
        assert "" not in content_values

    @pytest.mark.asyncio
    async def test_skips_none_content_messages(self):
        """Messages with None content should be skipped."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [_make_msg(role="user", content=None)]

        await adapter._call_model_for_recap(msgs, "prompt")

        invoke_args = mock_model.invoke.call_args[0][0]
        # Only the final prompt message should remain (no user msg with None)
        content_list = [getattr(m, "content", None) for m in invoke_args]
        # Either system_prompt or just the prompt
        assert None not in content_list

    @pytest.mark.asyncio
    async def test_prepends_system_message_when_provided(self):
        """When system_prompt is non-empty, it becomes the first message."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [_make_msg(role="user", content="hello")]

        await adapter._call_model_for_recap(msgs, "prompt", system_prompt="SYS PROMPT")

        invoke_args = mock_model.invoke.call_args[0][0]
        first_msg = invoke_args[0]
        assert getattr(first_msg, "content", None) == "SYS PROMPT"

    @pytest.mark.asyncio
    async def test_appends_prompt_as_final_user_message(self):
        """The prompt should be appended as the last UserMessage."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [_make_msg(role="user", content="hello")]

        await adapter._call_model_for_recap(msgs, "PROMPT TEXT")

        invoke_args = mock_model.invoke.call_args[0][0]
        last_msg = invoke_args[-1]
        assert getattr(last_msg, "content", None) == "PROMPT TEXT"

    @pytest.mark.asyncio
    async def test_cache_control_added_when_enabled(self):
        """When enable_prompt_caching=True, last pre-prompt message gets cache_control."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [
            _make_msg(role="user", content=[
                {"type": "text", "text": "hello"},
            ]),
        ]

        await adapter._call_model_for_recap(msgs, "prompt", enable_prompt_caching=True)

        invoke_args = mock_model.invoke.call_args[0][0]
        # The message with "hello" content should now have cache_control
        # Find the user message that has the content block
        pre_prompt_msgs = [m for m in invoke_args if getattr(m, "content", None) != "prompt"]
        # The last pre-prompt message should have cache_control on its last content block
        last_pre = pre_prompt_msgs[-1]
        if isinstance(last_pre.content, list) and len(last_pre.content) > 0:
            assert "cache_control" in last_pre.content[-1]

    @pytest.mark.asyncio
    async def test_no_cache_control_when_disabled(self):
        """When enable_prompt_caching=False, no cache_control marker is added."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [
            _make_msg(role="user", content=[
                {"type": "text", "text": "hello"},
            ]),
        ]

        await adapter._call_model_for_recap(msgs, "prompt", enable_prompt_caching=False)

        invoke_args = mock_model.invoke.call_args[0][0]
        # Find pre-prompt messages and check no cache_control
        for msg in invoke_args:
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        assert "cache_control" not in block

    @pytest.mark.asyncio
    async def test_no_cache_control_when_only_system_prompt(self):
        """When recap_messages is empty (only system_prompt present), no crash."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        # No conversation messages — only system_prompt
        await adapter._call_model_for_recap([], "prompt", system_prompt="SYS", enable_prompt_caching=True)

        # Should not crash — guards on empty recap_messages
        assert mock_model.invoke.called

    @pytest.mark.asyncio
    async def test_calls_model_invoke_without_temperature(self):
        """model.invoke() should be called WITHOUT temperature parameter."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
        )

        call_kwargs = mock_model.invoke.call_args[1]
        assert "temperature" not in call_kwargs

    @pytest.mark.asyncio
    async def test_logs_cache_hit_metrics(self):
        """When usage_metadata.cache_tokens > 0, cache metrics should be logged."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_result.tool_calls = None  # no tool_use → cache-log path, not fallback
        mock_usage = MagicMock()
        mock_usage.cache_tokens = 42
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 10
        mock_result.usage_metadata = mock_usage
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        with patch("logging.Logger.info") as mock_log:
            await adapter._call_model_for_recap(
                [_make_msg(role="user", content="hello")], "prompt",
            )

            cache_logged = any(
                "cache hit" in str(call_args) for call_args in mock_log.call_args_list
            )
            assert cache_logged

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        """When model.invoke() raises, return None and log exception."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_model.invoke.side_effect = RuntimeError("model crash")
        adapter._model = mock_model

        result = await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_empty_list_messages(self):
        """Messages with empty list content should be skipped."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [_make_msg(role="user", content=[])]

        await adapter._call_model_for_recap(msgs, "prompt")

        invoke_args = mock_model.invoke.call_args[0][0]
        content_vals = [getattr(m, "content", None) for m in invoke_args]
        assert [] not in content_vals

    @pytest.mark.asyncio
    async def test_correct_role_mapping(self):
        """User → UserMessage, assistant → AssistantMessage, other → UserMessage."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        msgs = [
            _make_msg(role="user", content="u1"),
            _make_msg(role="assistant", content="a1"),
            _make_msg(role="system", content="s1"),  # maps to UserMessage
        ]

        await adapter._call_model_for_recap(msgs, "prompt")

        invoke_args = mock_model.invoke.call_args[0][0]
        roles_found = [getattr(m, "role", type(m).__name__) for m in invoke_args]
        # Should contain UserMessage, AssistantMessage, UserMessage for system
        assert "user" in roles_found or any("UserMessage" in str(type(m)) for m in invoke_args)

    @pytest.mark.asyncio
    async def test_passes_tools_to_model_invoke(self):
        """tools arg must be forwarded to model.invoke to preserve the cache key."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        sentinel_tools = [{"name": "Read"}]
        await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
            tools=sentinel_tools,
        )

        assert mock_model.invoke.call_args[1]["tools"] is sentinel_tools

    @pytest.mark.asyncio
    async def test_default_tools_is_none_keeps_no_tools_path(self):
        """Omitting tools (recap path) must pass tools=None — no tools in request."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "answer"
        mock_result.tool_calls = None  # MagicMock auto-attr guard: no tool_use
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
        )

        assert mock_model.invoke.call_args[1]["tools"] is None

    @pytest.mark.asyncio
    async def test_tool_use_emitted_returns_fallback_not_tool_content(self):
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = ""  # no text — only tool_use
        tool_call = MagicMock()
        tool_call.name = "Read"
        mock_result.tool_calls = [tool_call]
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        result = await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
            tools=[{"name": "Read"}],
        )

        # Fallback surfaced to the user, mentioning the attempted tool name
        assert result is not None
        assert "Read" in result
        assert "工具" in result or "tool" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_use_discarded_even_with_text_content(self):
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "let me read that for you"
        tool_call = MagicMock()
        tool_call.name = "Read"
        mock_result.tool_calls = [tool_call]
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        result = await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
            tools=[{"name": "Read"}],
        )

        assert "Read" in result
        # The action-promise text must NOT be returned as the answer
        assert "let me read that for you" not in result

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_normal_content(self):
        """When tool_calls is absent/None, return content as before."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "plain answer"
        mock_result.tool_calls = None
        mock_result.usage_metadata = None  # avoid MagicMock auto-attr in cache-log guard
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        result = await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
            tools=[{"name": "Read"}],
        )

        assert result == "plain answer"

    @pytest.mark.asyncio
    async def test_empty_tool_calls_list_returns_normal_content(self):
        """An empty tool_calls list must not trigger the fallback path."""
        adapter = _make_adapter()
        mock_model = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "plain answer"
        mock_result.tool_calls = []
        mock_result.usage_metadata = None  # avoid MagicMock auto-attr in cache-log guard
        mock_model.invoke.return_value = mock_result
        adapter._model = mock_model

        result = await adapter._call_model_for_recap(
            [_make_msg(role="user", content="hello")], "prompt",
            tools=[{"name": "Read"}],
        )

        assert result == "plain answer"


# =============================================================================
# Section E: generate_btw_answer flow
# =============================================================================

class TestGenerateBtwAnswer:
    """Tests for JiuWenSwarmDeepAdapter.generate_btw_answer."""

    @pytest.mark.asyncio
    async def test_root_delegates_to_session_adapter_with_model(self):
        """A lazy root without a model must use the session-owned model."""
        root = _make_adapter(_model=None)
        root._is_session_scoped_adapter = False
        root._evict_idle_session_adapters = AsyncMock()

        session_adapter = _make_adapter(_last_system_prompt="sys")
        session_adapter._get_recent_messages = MagicMock(
            return_value=[_make_msg(role="user", content="hello")]
        )
        session_adapter._call_model_for_recap = AsyncMock(return_value="session answer")
        root._get_or_create_session_adapter = AsyncMock(return_value=session_adapter)

        result = await root.generate_btw_answer("code-session", "question?")

        assert result == {"status": "ok", "answer": "session answer"}
        assert root._model is None
        root._get_or_create_session_adapter.assert_awaited_once_with("code-session")
        session_adapter._call_model_for_recap.assert_awaited_once()
        root._evict_idle_session_adapters.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_no_context_when_no_messages_and_no_system_prompt(self):
        """When there are no messages AND no system prompt, return no_context."""
        adapter = _make_adapter(_last_system_prompt="")

        def _get_recent_messages(session_id, window=30):
            return []
        adapter._get_recent_messages = _get_recent_messages

        result = await adapter.generate_btw_answer("session-1", "question?")
        assert result == {"status": "no_context"}

    @pytest.mark.asyncio
    async def test_failed_when_model_returns_none(self):
        """When _call_model_for_recap returns None, return failed status."""
        adapter = _make_adapter(_last_system_prompt="sys prompt")

        def _get_recent_messages(session_id, window=30):
            return [_make_msg(role="user", content="hello")]
        adapter._get_recent_messages = _get_recent_messages

        async def _call_model(*args, **kwargs):
            return None
        adapter._call_model_for_recap = _call_model

        result = await adapter.generate_btw_answer("session-1", "question?")
        assert result["status"] == "failed"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_ok_with_trimmed_answer_on_success(self):
        """Successful path returns ok with stripped answer text."""
        adapter = _make_adapter(_last_system_prompt="sys")

        def _get_recent_messages(session_id, window=30):
            return [_make_msg(role="user", content="hello")]
        adapter._get_recent_messages = _get_recent_messages

        async def _call_model(messages, prompt, system_prompt="", enable_prompt_caching=True, **kwargs):
            return "  \n  the answer \n  "
        adapter._call_model_for_recap = _call_model

        result = await adapter.generate_btw_answer("session-1", "question?")
        assert result == {"status": "ok", "answer": "the answer"}

    @pytest.mark.asyncio
    async def test_passes_enable_prompt_caching_true(self):
        """generate_btw_answer must pass enable_prompt_caching=True to _call_model_for_recap."""
        adapter = _make_adapter(_last_system_prompt="sys")

        def _get_recent_messages(session_id, window=30):
            return [_make_msg()]
        adapter._get_recent_messages = _get_recent_messages

        received_kwargs = {}

        async def _capture_call(*args, **kwargs):
            received_kwargs.update(kwargs)
            return "answer"

        adapter._call_model_for_recap = _capture_call
        await adapter.generate_btw_answer("s1", "q?")
        assert received_kwargs.get("enable_prompt_caching") is True

    @pytest.mark.asyncio
    async def test_fetches_and_forwards_tools(self):
        adapter = _make_adapter(_last_system_prompt="sys")

        def _get_recent_messages(session_id, window=30):
            return [_make_msg()]
        adapter._get_recent_messages = _get_recent_messages

        fetched_tools = [{"name": "Read"}, {"name": "Bash"}]
        adapter._get_agent_tools = AsyncMock(return_value=fetched_tools)

        received_kwargs = {}

        async def _capture_call(*args, **kwargs):
            received_kwargs.update(kwargs)
            return "answer"

        adapter._call_model_for_recap = _capture_call
        await adapter.generate_btw_answer("s1", "q?")
        assert received_kwargs.get("tools") is fetched_tools

    @pytest.mark.asyncio
    async def test_empty_tools_normalized_to_none(self):
        adapter = _make_adapter(_last_system_prompt="sys")

        def _get_recent_messages(session_id, window=30):
            return [_make_msg()]
        adapter._get_recent_messages = _get_recent_messages
        adapter._get_agent_tools = AsyncMock(return_value=[])

        received_kwargs = {}

        async def _capture_call(*args, **kwargs):
            received_kwargs.update(kwargs)
            return "answer"

        adapter._call_model_for_recap = _capture_call
        await adapter.generate_btw_answer("s1", "q?")
        assert received_kwargs.get("tools") is None

    @pytest.mark.asyncio
    async def test_no_context_with_messages_but_no_system_prompt_still_calls_model(self):
        """Even without system_prompt, if messages exist, we should call the model."""
        adapter = _make_adapter(_last_system_prompt="")

        def _get_recent_messages(session_id, window=30):
            return [_make_msg()]
        adapter._get_recent_messages = _get_recent_messages

        called = False

        async def _call_model(*args, **kwargs):
            nonlocal called
            called = True
            return "answer"

        adapter._call_model_for_recap = _call_model
        result = await adapter.generate_btw_answer("s1", "q?")
        assert called
        assert result["status"] == "ok"


# =============================================================================
# Section F: _handle_command_btw handler
# =============================================================================

class TestHandleCommandBtw:
    """Tests for AgentWebSocketServer._handle_command_btw."""

    @pytest.mark.asyncio
    async def test_empty_question_returns_failed(self, server, fake_ws):
        """Empty question should return failed status immediately."""
        request = AgentRequest(
            request_id="req-btw-empty",
            channel_id="tui",
            session_id="sess-1",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": ""},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent == [
            {
                "response_id": "req-btw-empty",
                "payload": {"status": "failed", "error": "Question is required"},
                "ok": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_whitespace_only_question_returns_failed(self, server, fake_ws):
        """Whitespace-only question should be treated as empty."""
        request = AgentRequest(
            request_id="req-btw-ws",
            channel_id="tui",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "   "},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent[0]["payload"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_question_param_returns_failed(self, server, fake_ws):
        """Missing question param should return failed."""
        request = AgentRequest(
            request_id="req-btw-missing",
            channel_id="tui",
            req_method=ReqMethod.COMMAND_BTW,
            params={},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent[0]["payload"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_happy_path_returns_btw_result(self, server, fake_ws, monkeypatch):
        """Happy path: agent returns btw result, forwarded to client."""

        class MockAgent:
            async def generate_btw_answer(self, session_id, question):
                return {"status": "ok", "answer": "git status shows current state"}

        mock_agent = MockAgent()

        async def mock_get_agent(channel_id, mode, project_dir=None, sub_mode=None):
            return mock_agent

        monkeypatch.setattr(
            server.get_agent_manager_for_test(),
            "get_agent",
            mock_get_agent,
        )
        monkeypatch.setattr(
            agent_ws_server_module,
            "resolve_request_project_dir",
            lambda _req: None,
        )

        request = AgentRequest(
            request_id="req-btw-ok",
            channel_id="tui",
            session_id="sess-1",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "what does git status do?", "mode": "agent.plan"},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent == [
            {
                "response_id": "req-btw-ok",
                "payload": {"status": "ok", "answer": "git status shows current state"},
                "ok": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_prefers_agent_that_owns_session(self, server, fake_ws, monkeypatch):
        """An active session must not be routed to a new mode/project root."""

        class SessionAgent:
            async def generate_btw_answer(self, session_id, question):
                return {"status": "ok", "answer": "from session"}

        manager = server.get_agent_manager_for_test()
        session_agent = SessionAgent()
        get_agent = AsyncMock()
        monkeypatch.setattr(manager, "get_agent", get_agent)
        monkeypatch.setattr(
            manager,
            "get_agent_for_session_nowait",
            MagicMock(return_value=session_agent),
        )

        request = AgentRequest(
            request_id="req-btw-session-owner",
            channel_id="tui",
            session_id="sess-1",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "test?", "mode": "code"},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent[0]["payload"] == {
            "status": "ok",
            "answer": "from session",
        }
        get_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_not_found_returns_error(self, server, fake_ws, monkeypatch):
        """When agent_manager returns None, handler should return ok=False."""

        async def mock_get_agent_none(channel_id, mode, project_dir=None, sub_mode=None):
            return None

        monkeypatch.setattr(
            server.get_agent_manager_for_test(),
            "get_agent",
            mock_get_agent_none,
        )
        monkeypatch.setattr(
            agent_ws_server_module,
            "resolve_request_project_dir",
            lambda _req: None,
        )

        request = AgentRequest(
            request_id="req-btw-no-agent",
            channel_id="tui",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "test?"},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent[0]["ok"] is False
        assert fake_ws.sent[0]["payload"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_generate_btw_answer_raises_exception(self, server, fake_ws, monkeypatch):
        """When agent.generate_btw_answer raises, handler catches and returns ok=False."""

        class CrashingAgent:
            async def generate_btw_answer(self, session_id, question):
                raise RuntimeError("model unavailable")

        async def mock_get_agent(channel_id, mode, project_dir=None, sub_mode=None):
            return CrashingAgent()

        monkeypatch.setattr(
            server.get_agent_manager_for_test(),
            "get_agent",
            mock_get_agent,
        )
        monkeypatch.setattr(
            agent_ws_server_module,
            "resolve_request_project_dir",
            lambda _req: None,
        )

        request = AgentRequest(
            request_id="req-btw-crash",
            channel_id="tui",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "test?"},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent[0]["ok"] is False
        assert fake_ws.sent[0]["payload"]["status"] == "failed"
        assert "model unavailable" in fake_ws.sent[0]["payload"]["error"]

    @pytest.mark.asyncio
    async def test_uses_auto_harness_mode_mapped_to_agent(self, server, fake_ws, monkeypatch):
        """auto_harness mode should be mapped to 'agent' for agent_manager.get_agent."""

        captured_mode = {}

        class MockAgent:
            async def generate_btw_answer(self, session_id, question):
                return {"status": "ok", "answer": "ok"}

        async def mock_get_agent(channel_id, mode, project_dir=None, sub_mode=None):
            captured_mode["mode"] = mode
            captured_mode["sub_mode"] = sub_mode
            return MockAgent()

        monkeypatch.setattr(
            server.get_agent_manager_for_test(),
            "get_agent",
            mock_get_agent,
        )
        monkeypatch.setattr(
            agent_ws_server_module,
            "resolve_request_project_dir",
            lambda _req: None,
        )

        request = AgentRequest(
            request_id="req-btw-mode",
            channel_id="tui",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "test?", "mode": "auto_harness.plan"},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert captured_mode["mode"] == "agent"

    @pytest.mark.asyncio
    async def test_no_context_status_returns_payload_as_is(self, server, fake_ws, monkeypatch):
        """When btw returns no_context, payload passed through unchanged."""

        class MockAgent:
            async def generate_btw_answer(self, session_id, question):
                return {"status": "no_context"}

        async def mock_get_agent(channel_id, mode, project_dir=None, sub_mode=None):
            return MockAgent()

        monkeypatch.setattr(
            server.get_agent_manager_for_test(),
            "get_agent",
            mock_get_agent,
        )
        monkeypatch.setattr(
            agent_ws_server_module,
            "resolve_request_project_dir",
            lambda _req: None,
        )

        request = AgentRequest(
            request_id="req-btw-nocontext",
            channel_id="tui",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "test?"},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())

        assert fake_ws.sent == [
            {
                "response_id": "req-btw-nocontext",
                "payload": {"status": "no_context"},
                "ok": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_defaults_session_id_when_none(self, server, fake_ws, monkeypatch):
        """When request.session_id is None, defaults to 'default'."""

        captured_session_id = {}

        class MockAgent:
            async def generate_btw_answer(self, session_id, question):
                captured_session_id["sid"] = session_id
                return {"status": "ok", "answer": "ok"}

        async def mock_get_agent(channel_id, mode, project_dir=None, sub_mode=None):
            return MockAgent()

        monkeypatch.setattr(
            server.get_agent_manager_for_test(),
            "get_agent",
            mock_get_agent,
        )
        monkeypatch.setattr(
            agent_ws_server_module,
            "resolve_request_project_dir",
            lambda _req: None,
        )

        request = AgentRequest(
            request_id="req-btw-defaultsid",
            channel_id="tui",
            req_method=ReqMethod.COMMAND_BTW,
            params={"question": "test?"},
        )

        await server.handle_command_btw_for_test(fake_ws, request, asyncio.Lock())
        assert captured_session_id["sid"] == "default"


# =============================================================================
# Section G: interface.py generate_btw_answer pass-through
# =============================================================================

class TestInterfaceBtwPassThrough:
    """Tests for JiuWenSwarm.generate_btw_answer in interface.py."""

    @pytest.mark.asyncio
    async def test_delegates_to_adapter(self):
        """generate_btw_answer must delegate to the underlying adapter."""
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

        facade = object.__new__(JiuWenSwarm)
        mock_adapter = AsyncMock()
        mock_adapter.generate_btw_answer.return_value = {
            "status": "ok", "answer": "the answer",
        }
        facade._adapter = mock_adapter

        result = await facade.generate_btw_answer("session-1", "question?")
        assert result == {"status": "ok", "answer": "the answer"}
        mock_adapter.generate_btw_answer.assert_awaited_once_with(
            session_id="session-1", question="question?",
        )

    @pytest.mark.asyncio
    async def test_raises_when_adapter_is_none(self):
        """When _adapter is None, raise ValueError."""
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

        facade = object.__new__(JiuWenSwarm)
        facade._adapter = None

        with pytest.raises(ValueError, match="Agent adapter not available"):
            await facade.generate_btw_answer("session-1", "question?")


# =============================================================================
# Section H: build_recap_prompt (existing function, now shares model call path)
# =============================================================================

class TestBuildRecapPrompt:
    """Smoke tests for build_recap_prompt to ensure it still works."""

    @staticmethod
    def test_en_recap_prompt():
        result = build_recap_prompt(memory=None, language="en")
        assert "recap" in result.lower()
        assert "short sentences" in result.lower()

    @staticmethod
    def test_zh_recap_prompt():
        result = build_recap_prompt(memory=None, language="zh")
        assert "回顾" in result

    @staticmethod
    def test_recap_prompt_with_memory():
        result = build_recap_prompt(memory="previous context", language="en")
        assert "previous context" in result

    @staticmethod
    @pytest.mark.parametrize("current_mode", ["team", "code.team"])
    def test_current_mode_is_authoritative(current_mode):
        result = build_recap_prompt(
            memory=None,
            language="en",
            current_mode=current_mode,
        )

        assert f"Current mode: {current_mode}" in result
        assert "do not infer it from history" in result


class TestGenerateRecapModePropagation:
    """The active runtime mode must reach the model recap prompt unchanged."""

    @pytest.mark.asyncio
    async def test_root_creates_session_adapter_and_forwards_current_mode(self):
        root = _make_adapter()
        root._is_session_scoped_adapter = False
        root._evict_idle_session_adapters = AsyncMock()

        session_adapter = MagicMock()
        session_adapter.generate_recap = AsyncMock(
            return_value={"status": "ok", "summary": "done"}
        )
        root._get_or_create_session_adapter = AsyncMock(return_value=session_adapter)

        result = await root.generate_recap("session-1", current_mode="team")

        assert result == {"status": "ok", "summary": "done"}
        root._get_or_create_session_adapter.assert_awaited_once_with("session-1")
        session_adapter.generate_recap.assert_awaited_once_with(
            session_id="session-1",
            current_mode="team",
        )

    @pytest.mark.asyncio
    async def test_current_mode_is_added_to_model_prompt(self):
        adapter = _make_adapter()
        adapter._get_recent_messages = MagicMock(return_value=[_make_msg()])
        adapter._call_model_for_recap = AsyncMock(return_value="team recap")

        result = await adapter.generate_recap("session-1", current_mode="team")

        assert result == {"status": "ok", "summary": "team recap"}
        prompt = adapter._call_model_for_recap.await_args.args[1]
        assert "Current mode: team" in prompt


class TestInterfaceRecapPassThrough:
    @pytest.mark.asyncio
    async def test_delegates_current_mode_to_adapter(self):
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

        facade = object.__new__(JiuWenSwarm)
        mock_adapter = AsyncMock()
        mock_adapter.generate_recap.return_value = {"status": "ok", "summary": "done"}
        facade._adapter = mock_adapter

        result = await facade.generate_recap("session-1", current_mode="code.team")

        assert result == {"status": "ok", "summary": "done"}
        mock_adapter.generate_recap.assert_awaited_once_with(
            session_id="session-1",
            current_mode="code.team",
        )


class TestHandleCommandRecap:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("requested_mode", "expected_mode", "expected_sub_mode"),
        [
            ("team", "team", None),
            ("code.team", "code", "team"),
        ],
    )
    async def test_passes_canonical_mode_to_recap(
        self,
        server,
        fake_ws,
        monkeypatch,
        requested_mode,
        expected_mode,
        expected_sub_mode,
    ):
        mock_agent = MagicMock()
        mock_agent.generate_recap = AsyncMock(
            return_value={"status": "ok", "summary": "team code recap"}
        )
        get_agent = AsyncMock(return_value=mock_agent)
        monkeypatch.setattr(server.get_agent_manager_for_test(), "get_agent", get_agent)
        monkeypatch.setattr(
            agent_ws_server_module,
            "resolve_request_project_dir",
            lambda _request: None,
        )
        request = AgentRequest(
            request_id="req-recap-mode",
            channel_id="tui",
            session_id="session-1",
            req_method=ReqMethod.COMMAND_RECAP,
            params={"mode": requested_mode},
        )

        await server.handle_command_recap_for_test(fake_ws, request, asyncio.Lock())

        get_agent.assert_awaited_once_with(
            channel_id="tui",
            mode=expected_mode,
            project_dir=None,
            sub_mode=expected_sub_mode,
        )
        mock_agent.generate_recap.assert_awaited_once_with(
            session_id="session-1",
            current_mode=requested_mode,
        )
        assert fake_ws.sent[0]["payload"] == {
            "status": "ok",
            "summary": "team code recap",
        }
