"""External memory lifecycle and SDK integration for the flash adapter."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)


class RecordingAgent:
    def __init__(self):
        self.rails = []
        self.unregistered = []
        self.fail_unregister = False

    async def register_rail(self, rail):
        assert rail not in self.rails
        self.rails.append(rail)

    async def unregister_rail(self, rail):
        if self.fail_unregister:
            raise RuntimeError("unregister failed")
        self.rails.remove(rail)
        self.unregistered.append(rail)


@pytest.fixture
def memory_config(monkeypatch):
    config = {
        "memory": {
            "mode": "local",
            "engine": "external",
            "external": {"provider": "officeace_cloud", "user_id": "alice", "scope_id": "scope-a"},
            "office_ace_user_profile": {"endpoint": "https://memory.invalid", "api_key": "test-key"},
        },
    }
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "pc")
    # Keep these offline lifecycle tests independent of gateway DB overlays.
    from jiuwenswarm.agents.harness.common.memory import config as memory_module

    monkeypatch.setattr(memory_module, "get_memory_section", lambda cfg=None: (cfg or config)["memory"])
    return config


def make_adapter(monkeypatch, config, adapter_cls=JiuwenSwarmFlashAdapter):
    adapter = adapter_cls()
    adapter._instance = RecordingAgent()
    adapter._parent_session_id = "runtime-session"
    adapter._parent_thread_id = "business-thread"
    adapter._handle_memory_rail_by_config = AsyncMock()
    built = {"external": [], "profile": []}

    def build(kind):
        rail = SimpleNamespace(
            kind=kind,
            user_id=config["memory"]["external"]["user_id"],
            _provider=SimpleNamespace(on_session_end=AsyncMock()),
        )
        built[kind].append(rail)
        return rail

    monkeypatch.setattr(adapter, "_build_external_memory_rail", lambda: build("external"))
    monkeypatch.setattr(adapter, "_build_office_ace_user_profile_rail", lambda: build("profile"))
    return adapter, built


async def refresh(adapter):
    if isinstance(adapter, JiuwenSwarmFlashAdapter):
        await adapter._update_rails_for_mode("flash")
    else:
        await adapter._handle_external_memory_rail_by_config()
        await adapter._handle_office_ace_user_profile_rail_by_config()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,provider,expected", [
    ("external", "officeace_cloud", {"external", "profile"}),
    ("both", "officeace_cloud", {"external", "profile"}),
    ("builtin", "officeace_cloud", set()),
    ("none", "officeace_cloud", set()),
    ("external", "mem0", {"external"}),
    ("external", "", set()),
])
async def test_first_flash_request_obeys_engine_and_provider(monkeypatch, memory_config, engine, provider, expected):
    memory_config["memory"]["engine"] = engine
    memory_config["memory"]["external"]["provider"] = provider
    adapter, _ = make_adapter(monkeypatch, memory_config)
    await refresh(adapter)
    assert {rail.kind for rail in adapter._instance.rails} == expected
    first = list(adapter._instance.rails)
    await refresh(adapter)
    assert adapter._instance.rails == first
    assert not adapter._instance.unregistered


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [JiuwenSwarmFlashAdapter, JiuWenSwarmDeepAdapter])
async def test_identity_change_replaces_both_components(monkeypatch, memory_config, adapter_cls):
    adapter, built = make_adapter(monkeypatch, memory_config, adapter_cls)
    await refresh(adapter)
    first = list(adapter._instance.rails)
    memory_config["memory"]["external"]["user_id"] = "bob"
    await refresh(adapter)
    assert {rail.user_id for rail in adapter._instance.rails} == {"bob"}
    assert adapter._instance.unregistered == first
    assert all(len(rails) == 2 for rails in built.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [JiuwenSwarmFlashAdapter, JiuWenSwarmDeepAdapter])
async def test_disable_and_reenable_clear_registration_state(monkeypatch, memory_config, adapter_cls):
    adapter, _ = make_adapter(monkeypatch, memory_config, adapter_cls)
    await refresh(adapter)
    assert len(adapter._instance.rails) == 2
    memory_config["memory"]["engine"] = "none"
    await refresh(adapter)
    assert adapter._instance.rails == []
    assert not adapter._external_memory_rail_registered
    assert not adapter._office_ace_user_profile_rail_registered
    memory_config["memory"]["engine"] = "external"
    await refresh(adapter)
    assert len(adapter._instance.rails) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [JiuwenSwarmFlashAdapter, JiuWenSwarmDeepAdapter])
async def test_instance_replacement_does_not_reuse_stale_registration(monkeypatch, memory_config, adapter_cls):
    adapter, _ = make_adapter(monkeypatch, memory_config, adapter_cls)
    await refresh(adapter)
    previous = adapter._instance
    adapter._instance = RecordingAgent()
    await refresh(adapter)
    assert len(adapter._instance.rails) == 2
    assert previous.rails == []


@pytest.mark.asyncio
async def test_failed_disable_retains_rail_for_retry(monkeypatch, memory_config):
    adapter, _ = make_adapter(monkeypatch, memory_config, JiuWenSwarmDeepAdapter)
    await refresh(adapter)
    old = adapter._external_memory_rail
    adapter._instance.fail_unregister = True
    memory_config["memory"]["engine"] = "none"
    with pytest.raises(RuntimeError, match="unregister failed"):
        await refresh(adapter)
    assert adapter._external_memory_rail is old
    adapter._instance.fail_unregister = False
    await refresh(adapter)
    assert adapter._instance.rails == []


@pytest.mark.asyncio
async def test_switch_to_cloud_mode_releases_existing_builtin_memory(monkeypatch, memory_config):
    adapter = JiuwenSwarmFlashAdapter()
    adapter._instance = RecordingAgent()
    adapter._memory_rail = SimpleNamespace(kind="builtin")
    adapter._instance.rails.append(adapter._memory_rail)
    memory_config["memory"]["mode"] = "cloud"
    await adapter._handle_memory_rail_by_config("agent")
    assert adapter._memory_rail is None
    assert adapter._instance.rails == []


@pytest.mark.parametrize("field,value", [
    ("endpoint", "https://other-memory.invalid"),
    ("space_id", "other-space"),
    ("api_key", "other-credential"),
    ("user_id", "bob"),
])
def test_profile_cache_is_bound_to_service_identity(monkeypatch, tmp_path, field, value):
    from dataclasses import replace

    from jiuwenswarm.agents.harness.common.rails.office_ace_user_profile.fetcher import (
        UserProfileConfig,
        UserProfileFetcher,
    )

    monkeypatch.setenv("OFFICE_CLAW_GLOBAL_CONFIG_ROOT", str(tmp_path))
    config = UserProfileConfig(True, "https://memory.invalid", "test-key", "alice", space_id="space-a")
    first = UserProfileFetcher(config)
    first.cache_dir.mkdir(parents=True)
    first.cache_path.write_text("private profile", encoding="utf-8")
    second = UserProfileFetcher(replace(config, **{field: value}))
    assert second.read_cache() is None
    assert first.read_cache() == "private profile"


@pytest_asyncio.fixture
async def services(monkeypatch, tmp_path):
    import tiktoken
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.agents.harness.common.memory import external_memory_builder
    from jiuwenswarm.agents.harness.common.rails.office_ace_user_profile.fetcher import (
        UserProfileFetcher,
    )

    providers, fetches = [], []

    def build_provider(config):
        user = config["user_id"]
        provider = SimpleNamespace(
            name="officeace_cloud",
            initialize=AsyncMock(),
            prefetch=AsyncMock(return_value=f"remembered-fact-for-{user}"),
            sync_turn=AsyncMock(),
            shutdown=AsyncMock(),
            on_session_end=AsyncMock(),
            get_tool_schemas=lambda: [{
                "name": "external_memory_search", "description": "Search remembered facts",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            }],
            handle_tool_call=AsyncMock(return_value=json.dumps({"result": f"tool-memory-for-{user}"})),
            system_prompt_block=lambda: "Call external_memory_search for recalled facts.",
        )
        providers.append(provider)
        return provider

    async def fetch_profile(fetcher):
        fetches.append(fetcher.config.user_id)
        content = f"profile-for-{fetcher.config.user_id}"
        fetcher._write_cache_atomic(content)
        return content

    monkeypatch.setenv("OFFICE_CLAW_GLOBAL_CONFIG_ROOT", str(tmp_path))
    # No tokenizer downloads: this suite checks content delivery, not token budgets.
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: SimpleNamespace(
        encode=lambda text, **kwargs: range(len(text) // 4),
    ))
    monkeypatch.setattr(external_memory_builder, "_build_officeace_cloud_provider", build_provider)
    monkeypatch.setattr(UserProfileFetcher, "fetch_once", fetch_profile)
    yield SimpleNamespace(providers=providers, fetches=fetches)
    await Runner.stop()


async def make_sdk_adapter(monkeypatch, adapter_cls=JiuwenSwarmFlashAdapter, user="alice"):
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig

    adapter = adapter_cls()
    agent = DeepAgent(AgentCard(id="flash-memory-" + uuid4().hex, name="flash-memory-test"))
    agent.configure(DeepAgentConfig(
        rails=[], enable_task_loop=False, enable_read_image_multimodal=False,
        system_prompt="Memory integration test", max_iterations=5,
    ))
    await agent.ensure_initialized()
    adapter._instance = agent
    adapter._parent_session_id = f"runtime-{user}"
    adapter._parent_thread_id = f"business-{user}"
    monkeypatch.setattr(adapter, "_handle_memory_rail_by_config", AsyncMock())
    await refresh(adapter)
    return adapter


def capture_model(monkeypatch, agent, *, use_tool=False):
    from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall

    calls = []

    async def invoke(messages, *, tools=None, **kwargs):
        calls.append((messages, tools))
        if use_tool and len(calls) == 1:
            return AssistantMessage(content="", tool_calls=[ToolCall(
                id="memory-query", type="function", name="external_memory_search",
                arguments='{"query": "earlier preference"}',
            )])
        return AssistantMessage(content="memory answer")

    monkeypatch.setattr(agent._react_agent, "_get_llm", lambda: SimpleNamespace(invoke=invoke))
    return calls


def model_text(call):
    return "\n".join(str(getattr(message, "content", message)) for message in call[0])


async def release_services(adapter):
    async with adapter._memory_service_rail_lock:
        await adapter._release_memory_service_rail("_external_memory_rail")
        await adapter._release_memory_service_rail("_office_ace_user_profile_rail")


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [JiuwenSwarmFlashAdapter, JiuWenSwarmDeepAdapter])
async def test_real_model_pipeline_receives_memory_profile_and_tool(monkeypatch, memory_config, services, adapter_cls):
    adapter = await make_sdk_adapter(monkeypatch, adapter_cls)
    provider = services.providers[0]
    calls = capture_model(monkeypatch, adapter._instance, use_tool=True)
    try:
        await adapter._instance.invoke({"query": "Recall my preferences"})
        assert len(calls) == 2
        assert "remembered-fact-for-alice" in model_text(calls[0])
        assert "profile-for-alice" in model_text(calls[0])
        assert "&lt;memory-context&gt;" in model_text(calls[0])
        assert "external_memory_search" in str(calls[0][1])
        assert "tool-memory-for-alice" in model_text(calls[1])
        provider.handle_tool_call.assert_awaited_once_with("external_memory_search", {"query": "earlier preference"})
        assert provider.initialize.await_args.kwargs == {
            "user_id": "alice", "scope_id": "scope-a", "session_id": "runtime-alice", "thread_id": "business-alice",
        }
        assert services.fetches == ["alice"]
    finally:
        await release_services(adapter)
    provider.sync_turn.assert_awaited_once()
    assert provider.sync_turn.await_args.kwargs["thread_id"] == "business-alice"
    provider.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_reload_and_disable_clean_model_input(monkeypatch, memory_config, services):
    from openjiuwen.harness.schema.config import DeepAgentConfig

    adapter = await make_sdk_adapter(monkeypatch)
    agent = adapter._instance
    calls = capture_model(monkeypatch, agent)
    try:
        await agent.invoke({"query": "first"})
        agent.configure(DeepAgentConfig(
            rails=[], tools=[], system_prompt="new prompt", enable_task_loop=False, enable_read_image_multimodal=False,
        ))
        await agent.ensure_initialized()
        await refresh(adapter)
        await agent.invoke({"query": "second"})
        assert "remembered-fact-for-alice" in model_text(calls[-1])
        assert "profile-for-alice" in model_text(calls[-1])
        assert "Call external_memory_search" in model_text(calls[-1])
        assert len(services.providers) == 1
        assert services.fetches == ["alice"]
        memory_config["memory"]["engine"] = "none"
        await refresh(adapter)
        await agent.invoke({"query": "third"})
        assert "remembered-fact-for-alice" not in model_text(calls[-1])
        assert "profile-for-alice" not in model_text(calls[-1])
        assert "external_memory_search" not in str(calls[-1][1])
    finally:
        await release_services(adapter)


@pytest.mark.asyncio
async def test_two_live_agents_keep_provider_tools_and_profiles_separate(monkeypatch, memory_config, services):
    first = await make_sdk_adapter(monkeypatch)
    memory_config["memory"]["external"]["user_id"] = "bob"
    second = await make_sdk_adapter(monkeypatch, user="bob")
    first_calls = capture_model(monkeypatch, first._instance, use_tool=True)
    second_calls = capture_model(monkeypatch, second._instance, use_tool=True)
    try:
        await asyncio.gather(
            first._instance.invoke({"query": "my history"}),
            second._instance.invoke({"query": "my history"}),
        )
        for calls, user, other in [(first_calls, "alice", "bob"), (second_calls, "bob", "alice")]:
            assert f"profile-for-{user}" in model_text(calls[0])
            assert f"tool-memory-for-{user}" in model_text(calls[1])
            assert f"-for-{other}" not in "\n".join(model_text(call) for call in calls)
    finally:
        await release_services(first)
        await release_services(second)


@pytest.mark.asyncio
async def test_group_memory_disabled_removes_tools_injection_and_reporting(monkeypatch, memory_config, services):
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        TOOL_PERMISSION_CONTEXT,
        PermissionContext,
    )

    adapter = await make_sdk_adapter(monkeypatch)
    token = TOOL_PERMISSION_CONTEXT.set(PermissionContext(
        group_digital_avatar=True, avatar_mode=True, enable_memory=False,
    ))
    try:
        await refresh(adapter)
        calls = capture_model(monkeypatch, adapter._instance)
        await adapter._instance.invoke({"query": "private task"})
        assert "remembered-fact" not in model_text(calls[0])
        assert "profile-for" not in model_text(calls[0])
        assert "external_memory_search" not in str(calls[0][1])
        services.providers[0].prefetch.assert_not_awaited()
        services.providers[0].sync_turn.assert_not_awaited()
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)
        await release_services(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_state", ["fresh", "stale", "missing"])
@pytest.mark.parametrize("fetch_fails", [False, True])
async def test_profile_cache_refresh_and_failure_fallback(monkeypatch, memory_config, services, cache_state, fetch_fails):
    adapter = await make_sdk_adapter(monkeypatch)
    fetcher = adapter._office_ace_user_profile_rail._fetcher
    if cache_state != "missing":
        fetcher._write_cache_atomic("cached-profile")
        if cache_state == "stale":
            os.utime(fetcher.cache_path, (0, 0))
    fetch = AsyncMock(side_effect=RuntimeError("unavailable")) if fetch_fails else AsyncMock(return_value="new-profile")
    monkeypatch.setattr(fetcher, "fetch_once", fetch)
    calls = capture_model(monkeypatch, adapter._instance)
    try:
        await adapter._instance.invoke({"query": "my preferences"})
        if cache_state == "fresh":
            assert "cached-profile" in model_text(calls[0])
            fetch.assert_not_awaited()
        else:
            fetch.assert_awaited_once()
            expected = "cached-profile" if fetch_fails else "new-profile"
            if cache_state == "missing" and fetch_fails:
                assert "office_ace_user_profile" not in model_text(calls[0])
            else:
                assert expected in model_text(calls[0])
    finally:
        await release_services(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "error", "timeout", "initialize"])
async def test_external_service_degrades_without_stale_recall(monkeypatch, memory_config, services, failure):
    adapter = await make_sdk_adapter(monkeypatch)
    provider = services.providers[0]
    calls = capture_model(monkeypatch, adapter._instance)
    try:
        if failure == "initialize":
            provider.initialize.side_effect = RuntimeError("init unavailable")
        else:
            await adapter._instance.invoke({"query": "first question"})
            assert "remembered-fact-for-alice" in model_text(calls[-1])
            if failure == "empty":
                provider.prefetch.return_value = ""
            elif failure == "error":
                provider.prefetch.side_effect = RuntimeError("search unavailable")
            else:
                async def slow_search(*args, **kwargs):
                    await asyncio.Event().wait()

                provider.prefetch.side_effect = slow_search
                adapter._external_memory_rail.PREFETCH_TIMEOUT = 0.01
        await adapter._instance.invoke({"query": "different question"})
        assert "remembered-fact-for-alice" not in model_text(calls[-1])
        assert "profile-for-alice" in model_text(calls[-1])
        if failure == "initialize":
            provider.prefetch.assert_not_awaited()
    finally:
        await release_services(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_kind", ["cron", "heartbeat"])
async def test_background_runs_keep_context_without_reporting(monkeypatch, memory_config, services, run_kind):
    adapter = await make_sdk_adapter(monkeypatch)
    calls = capture_model(monkeypatch, adapter._instance)
    try:
        await adapter._instance.invoke({"query": "scheduled task", "run_kind": run_kind})
        assert "remembered-fact-for-alice" in model_text(calls[-1])
        services.providers[0].sync_turn.assert_not_awaited()
    finally:
        await release_services(adapter)


@pytest.mark.asyncio
async def test_sdk_removed_rail_is_registered_again(monkeypatch, memory_config, services):
    adapter = await make_sdk_adapter(monkeypatch)
    old = adapter._external_memory_rail
    await adapter._instance.unregister_rail(old)
    await refresh(adapter)
    try:
        assert adapter._external_memory_rail is not old
        assert adapter._instance.is_registered_rail(adapter._external_memory_rail)
        calls = capture_model(monkeypatch, adapter._instance)
        await adapter._instance.invoke({"query": "remember me"})
        assert "remembered-fact-for-alice" in model_text(calls[-1])
    finally:
        await release_services(adapter)


@pytest.mark.asyncio
async def test_progressive_discovery_can_load_external_memory(monkeypatch, memory_config, services):
    from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall

    adapter = await make_sdk_adapter(monkeypatch)
    agent = adapter._instance
    monkeypatch.setattr(adapter, "_tool_owner_id", lambda: agent.card.id)
    progressive = adapter._build_progressive_tool_rail({"tool_lazy_load": {"enabled": True, "eager_tools": []}})
    assert progressive is not None
    await agent.register_rail(progressive)
    calls = []

    async def invoke(messages, *, tools=None, **kwargs):
        calls.append((messages, tools))
        index = len(calls)
        if index == 1:
            name, args = "tools_search", {"tool_name": "external_memory_search"}
        elif index == 2:
            name, args = "invoke_tool", {"tool_name": "external_memory_search", "arguments": {"query": "history"}}
        else:
            return AssistantMessage(content="done")
        return AssistantMessage(content="", tool_calls=[ToolCall(
            id=f"discovery-{index}", type="function", name=name, arguments=json.dumps(args),
        )])

    monkeypatch.setattr(agent._react_agent, "_get_llm", lambda: SimpleNamespace(invoke=invoke))
    try:
        await agent.invoke({"query": "find my history"})
        services.providers[0].handle_tool_call.assert_awaited_once_with("external_memory_search", {"query": "history"})
        assert "input_schema" in model_text(calls[1])
        assert "tool-memory-for-alice" in model_text(calls[2])
    finally:
        await agent.unregister_rail(progressive)
        await release_services(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["recall", "profile"])
async def test_request_cancellation_releases_memory_resources(monkeypatch, memory_config, services, stage):
    adapter = await make_sdk_adapter(monkeypatch)
    provider = services.providers[0]
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_service(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "recall":
        provider.prefetch.side_effect = slow_service
    else:
        monkeypatch.setattr(adapter._office_ace_user_profile_rail._fetcher, "fetch_once", slow_service)
    calls = capture_model(monkeypatch, adapter._instance)
    request = asyncio.create_task(adapter._instance.invoke({"query": "cancelled request"}))
    await asyncio.wait_for(started.wait(), timeout=1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await release_services(adapter)
    assert cancelled.is_set()
    assert calls == []
    assert not await adapter._instance.prompt_attachment_manager.collect_for_session("default_session")
    provider.shutdown.assert_awaited_once()
