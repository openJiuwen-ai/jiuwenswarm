# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    Model,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentRail,
    ToolCallInputs,
)
from jiuwenswarm.agents.harness.common.browser_defaults import (
    DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.common.schema.agent import AgentRequest


class MockLLMModel:
    def __init__(self) -> None:
        self.responses: list[AssistantMessage] = []
        self.call_count = 0

    def set_responses(self, responses: list[AssistantMessage]) -> None:
        self.responses = responses
        self.call_count = 0

    def _next_response(self) -> AssistantMessage:
        if self.call_count < len(self.responses):
            response = self.responses[self.call_count]
            self.call_count += 1
            return response
        return AssistantMessage(content="Default mock response")

    async def invoke(self, messages, **kwargs):
        del messages, kwargs
        return self._next_response()

    async def stream(self, messages, **kwargs):
        del messages, kwargs
        result = self._next_response()
        yield AssistantMessageChunk(
            content=result.content,
            tool_calls=result.tool_calls,
            usage_metadata=result.usage_metadata,
        )


class ToolTraceRail(AgentRail):
    def __init__(self) -> None:
        super().__init__()
        self.tool_calls: list[str] = []

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if isinstance(ctx.inputs, ToolCallInputs) and ctx.inputs.tool_name:
            self.tool_calls.append(ctx.inputs.tool_name)


class _TestableJiuWenSwarmDeepAdapter(JiuWenSwarmDeepAdapter):
    def set_workspace_dir(self, workspace_dir: str) -> None:
        self._workspace_dir = workspace_dir

    def build_configured_subagents(
        self,
        model: Model,
        config: dict,
        config_base: dict | None = None,
    ):
        return self._build_configured_subagents(model, config, config_base)


def create_text_response(content: str) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        usage_metadata=UsageMetadata(model_name="mock-model", finish_reason="stop"),
    )


def create_tool_call_response(
    tool_name: str,
    arguments: str,
    *,
    tool_call_id: str,
) -> AssistantMessage:
    return AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id=tool_call_id,
                type="function",
                name=tool_name,
                arguments=arguments,
            )
        ],
        usage_metadata=UsageMetadata(model_name="mock-model", finish_reason="tool_calls"),
    )


def _build_model() -> Model:
    return Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_key="test-key",
            api_base="https://example.invalid/v1",
            verify_ssl=False,
        ),
        model_config=ModelRequestConfig(model_name="mock-model"),
    )


def _make_fake_runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.ensure_started = AsyncMock()
    runtime.service = MagicMock()
    runtime.service.run_task = AsyncMock()
    runtime.controller = MagicMock()
    runtime.controller.bind_runtime = MagicMock()
    runtime.controller.bind_code_executor = MagicMock()
    runtime.controller.run_action = AsyncMock()
    runtime.code_executor = None
    return runtime


# ─── DeepAdapter (agent mode): only research_agent + browser_agent ──────

def test_deep_adapter_keeps_builtin_statusline_agent_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _TestableJiuWenSwarmDeepAdapter()
    model = _build_model()
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_browser_runtime_enabled",
        staticmethod(lambda: False),
    )
    # The built-in setup agent is available even in an older config that has no
    # explicit subagents section.
    subagents, _ = adapter.build_configured_subagents(
        model,
        {"max_iterations": 8},
        {},
    )

    assert subagents is not None
    assert [item.agent_card.name for item in subagents] == ["statusline-setup"]


def test_browser_runtime_environment_tracks_mode_and_chrome_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _TestableJiuWenSwarmDeepAdapter()
    monkeypatch.setenv("BROWSER_MANAGED_BINARY", "C:\\stale\\chrome.exe")
    monkeypatch.setenv(
        "PLAYWRIGHT_MCP_ARGS",
        "-y @playwright/mcp@latest --headless",
    )

    adapter._sync_browser_runtime_environment(
        {
            "browser": {
                "chrome_path": "C:\\custom\\chrome.exe",
                "headless": False,
            }
        }
    )

    assert os.environ["BROWSER_MANAGED_BINARY"] == "C:\\custom\\chrome.exe"
    assert "BROWSER_MANAGED_ARGS" not in os.environ
    assert "--headless" not in os.environ["PLAYWRIGHT_MCP_ARGS"].split()

    adapter._sync_browser_runtime_environment(
        {"browser": {"chrome_path": "", "headless": True}}
    )

    assert "BROWSER_MANAGED_BINARY" not in os.environ
    assert os.environ["BROWSER_MANAGED_ARGS"] == "--headless=new"
    assert os.environ["PLAYWRIGHT_MCP_ARGS"].split().count("--headless") == 1


def test_deep_adapter_subagents_includes_browser_by_default_when_runtime_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter.set_workspace_dir("/tmp/test-workspace")
    model = _build_model()

    monkeypatch.setattr(
        deep_interface_module,
        "build_browser_agent_config",
        lambda *args, **kwargs: {"name": "browser_agent", "kwargs": kwargs},
    )
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_browser_runtime_enabled",
        staticmethod(lambda: True),
    )
    monkeypatch.setenv("BROWSER_DRIVER", "managed")

    subagents, _ = adapter.build_configured_subagents(
        model,
        {"max_iterations": 8},
        {},
    )

    assert subagents is not None
    assert [
        item.agent_card.name if hasattr(item, "agent_card") else item["name"]
        for item in subagents
    ] == ["statusline-setup", "browser_agent"]
    assert (
        subagents[-1]["kwargs"]["max_iterations"]
        == DEFAULT_BROWSER_AGENT_MAX_ITERATIONS
    )


def test_deep_adapter_subagents_only_includes_explicitly_enabled_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter.set_workspace_dir("/tmp/test-workspace")
    model = _build_model()

    monkeypatch.setattr(
        deep_interface_module,
        "build_research_agent_config",
        lambda *args, **kwargs: {"name": "research_agent", "kwargs": kwargs},
    )
    monkeypatch.setattr(
        deep_interface_module,
        "build_browser_agent_config",
        lambda *args, **kwargs: {"name": "browser_agent", "kwargs": kwargs},
    )
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_browser_runtime_enabled",
        staticmethod(lambda: True),
    )
    monkeypatch.setenv("BROWSER_DRIVER", "managed")

    subagents, _ = adapter.build_configured_subagents(
        model,
        {
            "max_iterations": 8,
            "subagents": {
                "research_agent": {"enabled": True, "max_iterations": 5},
                "browser_agent": {"max_iterations": 7},
            },
        },
        {},
    )

    assert subagents is not None
    names = [
        item.agent_card.name if hasattr(item, "agent_card") else item["name"]
        for item in subagents
    ]
    assert names == [
        "statusline-setup",
        "research_agent",
        "browser_agent",
    ]
    assert subagents[1]["kwargs"]["max_iterations"] == 5
    assert subagents[2]["kwargs"]["max_iterations"] == 7


def test_deep_adapter_subagents_skips_browser_without_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _TestableJiuWenSwarmDeepAdapter()
    model = _build_model()

    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_browser_runtime_enabled",
        staticmethod(lambda: False),
    )
    browser_builder = MagicMock()
    monkeypatch.setattr(deep_interface_module, "build_browser_agent_config", browser_builder)

    # The browser is skipped, while the default-enabled status-line setup
    # subagent remains available.
    subagents, _ = adapter.build_configured_subagents(
        model,
        {
            "subagents": {
                "browser_agent": {"max_iterations": 7},
            },
        },
        {},
    )

    assert subagents is not None
    assert [item.agent_card.name for item in subagents] == ["statusline-setup"]
    browser_builder.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skip(reason="Browser subagent integration test is flaky")
async def test_interface_deep_browser_subagent_task_tool_chain(
    temp_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_RUNTIME_MCP_ENABLED", "1")
    monkeypatch.setenv("BROWSER_RUNTIME_MCP_ENABLED", "1")
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("MODEL_NAME", "mock-model")
    monkeypatch.setenv("MODEL_PROVIDER", "OpenAI")

    config_base = {
        "preferred_language": "en",
        "react": {
            "agent_name": "main_agent",
            "enable_task_loop": False,
            "max_iterations": 8,
        },
    }
    monkeypatch.setattr(deep_interface_module, "get_config", lambda: config_base)

    mock_llm = MockLLMModel()
    mock_llm.set_responses([
        create_tool_call_response(
            "task_tool",
            (
                '{"subagent_type": "browser_agent", '
                '"task_description": "Open https://example.com and summarize the page"}'
            ),
            tool_call_id="main_task_tool_call",
        ),
        create_tool_call_response(
            "browser_run_task",
            (
                '{"task": "Open https://example.com and summarize the page", '
                '"session_id": "browser-sub-session"}'
            ),
            tool_call_id="browser_run_task_call",
        ),
        create_text_response("Browser subagent finished: page title is Example Domain."),
        create_text_response("Main agent received browser result successfully."),
    ])
    model = _build_model()
    runtime = _make_fake_runtime()
    runtime.service.run_task.return_value = {
        "ok": True,
        "session_id": "browser-sub-session",
        "final": "page title is Example Domain.",
        "page": {"url": "https://example.com", "title": "Example Domain"},
        "screenshot": None,
        "error": None,
    }
    tool_trace = ToolTraceRail()
    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter.set_workspace_dir(str(temp_workspace / "workspace" / "agent"))

    with (
        patch.object(JiuWenSwarmDeepAdapter, "set_checkpoint", AsyncMock()),
        patch.object(JiuWenSwarmDeepAdapter, "_create_model", return_value=model),
        patch.object(JiuWenSwarmDeepAdapter, "_get_tool_cards", AsyncMock(return_value=[])),
        patch.object(JiuWenSwarmDeepAdapter, "_build_agent_rails", return_value=[tool_trace]),
        patch.object(JiuWenSwarmDeepAdapter, "_create_sys_operation", return_value=MagicMock()),
        patch.object(JiuWenSwarmDeepAdapter, "_proc_context_compaction", AsyncMock()),
        patch.object(JiuWenSwarmDeepAdapter, "_register_runtime_tools", AsyncMock()),
        patch.object(JiuWenSwarmDeepAdapter, "_refresh_multimodal_configs", return_value=None),
        patch(
            "openjiuwen.harness.subagents.browser_agent.BrowserAgentRuntime",
            return_value=runtime,
        ),
        patch("openjiuwen.core.foundation.llm.model.Model.invoke", side_effect=mock_llm.invoke),
        patch("openjiuwen.core.foundation.llm.model.Model.stream", side_effect=mock_llm.stream),
    ):
        await Runner.start()
        try:
            await adapter.create_instance()
            request = AgentRequest(
                request_id="req-browser-1",
                channel_id="web",
                session_id="sess-browser-1",
                params={
                    "query": "Use the browser agent to inspect https://example.com.",
                    "mode": "agent.fast",
                },
            )
            response = await adapter.process_message_impl(
                request,
                {
                    "query": request.params["query"],
                    "conversation_id": request.session_id,
                    "request_id": request.request_id,
                },
            )
        finally:
            await Runner.stop()

    assert response.ok is True
    payload = response.payload["content"]
    assert payload["result_type"] == "answer"
    assert "browser result" in payload["output"].lower()
    assert "task_tool" in tool_trace.tool_calls
    assert runtime.ensure_started.await_count >= 1
    runtime.service.run_task.assert_awaited_once()
    run_task_kwargs = runtime.service.run_task.await_args.kwargs
    assert run_task_kwargs["task"] == "Open https://example.com and summarize the page"
