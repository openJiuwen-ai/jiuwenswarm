from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _Context:
    def __init__(self, token_counter):
        self._token_counter = token_counter

    def token_counter(self):
        return self._token_counter

    @staticmethod
    def get_messages():
        return [SimpleNamespace(content="hello")]


def _make_adapter(*, token_counter, tools):
    context = _Context(token_counter)
    react_agent = SimpleNamespace(
        context_engine=SimpleNamespace(get_context=lambda session_id: context),
        ability_manager=SimpleNamespace(list=lambda: tools),
    )
    instance = SimpleNamespace(
        react_agent=react_agent,
        get_context_usage=lambda session_id: {
            "total_tokens": 0,
            "context_window_tokens": 1000,
            "usage_percent": 0,
        },
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._instance = instance
    adapter._get_agent_system_prompt = lambda: "system prompt"
    return adapter


@pytest.mark.asyncio
async def test_context_usage_estimates_registered_tools_without_token_counter():
    adapter = _make_adapter(
        token_counter=None,
        tools=[
            SimpleNamespace(
                name="read_file",
                description="Read a file from disk",
                input_params={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            )
        ],
    )

    result = await adapter.get_context_usage("session-1")

    assert result["tools_tokens"] > 0
    assert result["total_tokens"] == (
        result["system_prompt_tokens"]
        + result["messages_tokens"]
        + result["tools_tokens"]
    )


@pytest.mark.asyncio
async def test_context_usage_keeps_token_counter_tool_count():
    token_counter = MagicMock()
    token_counter.count.return_value = 11
    token_counter.count_messages.return_value = 22
    token_counter.count_tools.return_value = 33
    adapter = _make_adapter(
        token_counter=token_counter,
        tools=[SimpleNamespace(name="read_file", description="Read", input_params={})],
    )

    result = await adapter.get_context_usage("session-1")

    assert result["tools_tokens"] == 33
    token_counter.count_tools.assert_called_once()


@pytest.mark.asyncio
async def test_context_usage_keeps_zero_tools_when_none_are_registered():
    adapter = _make_adapter(token_counter=None, tools=[])

    result = await adapter.get_context_usage("session-1")

    assert result["tools_tokens"] == 0
