# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the per-stage timing of the per-request runtime setup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.core.memory.lite.manager import MemoryIndexManager

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _RecordingLogger:
    """Logger double capturing the level and formatted message of each call.

    Levels are tracked separately because both breakdown branches now go to
    the same logger: which one ran is a question about level, not about which
    logger object received the line.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, tuple]] = []
        self.info_records: list[tuple[str, tuple]] = []
        self.debug_records: list[tuple[str, tuple]] = []

    def info(self, msg: str, *args) -> None:
        self.records.append((msg, args))
        self.info_records.append((msg, args))

    def debug(self, msg: str, *args, **kwargs) -> None:
        self.records.append((msg, args))
        self.debug_records.append((msg, args))


def _make_adapter(stages: list[str]) -> JiuWenSwarmDeepAdapter:
    """Create an adapter whose runtime-config stages are stubbed to record order."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = object()

    async def _stages(runtime_config, stage_timer, *, bind_request) -> None:
        for stage in stages:
            stage_timer.mark(stage)

    adapter._apply_runtime_config_stages = _stages
    return adapter


@pytest.fixture(name="loggers")
def _loggers(monkeypatch: pytest.MonkeyPatch) -> tuple[_RecordingLogger, _RecordingLogger]:
    server_log = _RecordingLogger()
    module_log = _RecordingLogger()
    monkeypatch.setattr(interface_deep, "server_logger", server_log)
    monkeypatch.setattr(interface_deep, "logger", module_log)
    return server_log, module_log


@pytest.mark.asyncio
async def test_uninitialized_adapter_still_rejects_the_turn(loggers) -> None:
    """The timing wrapper must not swallow the not-initialized contract."""
    adapter = _make_adapter([])
    adapter._instance = None

    with pytest.raises(RuntimeError):
        await adapter._update_runtime_config(object())


@pytest.mark.asyncio
async def test_stage_breakdown_is_logged_for_the_turn(loggers) -> None:
    """Every stage that ran must appear in the emitted breakdown."""
    server_log, module_log = loggers
    adapter = _make_adapter(["cwd_seed", "rails_for_mode", "session_tools"])
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    await adapter._update_runtime_config(runtime_config)

    records = server_log.records + module_log.records
    assert len(records) == 1
    breakdown = records[0][1][-1]
    assert "cwd_seed=" in breakdown
    assert "rails_for_mode=" in breakdown
    assert "session_tools=" in breakdown


@pytest.mark.asyncio
async def test_slow_turn_is_reported_at_info(loggers, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crossing the threshold routes the breakdown to the server log stream."""
    server_log, module_log = loggers
    monkeypatch.setattr(interface_deep, "_SLOW_RUNTIME_CONFIG_MS", 0.0)
    adapter = _make_adapter(["cwd_seed"])
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    await adapter._update_runtime_config(runtime_config)

    assert len(server_log.info_records) == 1
    assert server_log.debug_records == []


@pytest.mark.asyncio
async def test_fast_turn_stays_at_debug(loggers, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cheap turn must not add an INFO line to every request."""
    server_log, module_log = loggers
    monkeypatch.setattr(interface_deep, "_SLOW_RUNTIME_CONFIG_MS", 10_000.0)
    adapter = _make_adapter(["cwd_seed"])
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    await adapter._update_runtime_config(runtime_config)

    assert server_log.info_records == []
    assert len(server_log.debug_records) == 1


@pytest.mark.asyncio
async def test_failing_stage_still_reports_how_far_it_got(loggers) -> None:
    """A raising turn is exactly when the breakdown matters most."""
    server_log, module_log = loggers
    adapter = _make_adapter([])

    async def _stages(runtime_config, stage_timer, *, bind_request) -> None:
        stage_timer.mark("cwd_seed")
        stage_timer.mark("rail_setters")
        raise ValueError("boom")

    adapter._apply_runtime_config_stages = _stages
    runtime_config = type("_Cfg", (), {"session_id": "sess_a", "mode": "agent"})()

    with pytest.raises(ValueError):
        await adapter._update_runtime_config(runtime_config)

    records = server_log.records + module_log.records
    assert len(records) == 1
    breakdown = records[0][1][-1]
    assert "cwd_seed=" in breakdown
    assert "rail_setters=" in breakdown
    # The stage that raised never closed, so it must not appear as completed.
    assert "runtime_state=" not in breakdown


class _EmbeddingProvider:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self.started = None
        self.release = None

    async def embed_query(self, _text):
        self.calls += 1
        if self.error:
            raise self.error
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return self.result


@pytest.mark.asyncio
async def test_memory_embedding_probe_recovers_after_cooldown(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(interface_deep, "_memory_embedding_now", lambda: clock[0])
    rail = object.__new__(interface_deep._EmbeddingHealthMemoryRail)
    rail._embedding_health = interface_deep._MemoryEmbeddingHealth()
    provider = _EmbeddingProvider(error=RuntimeError("model not found"))
    manager = SimpleNamespace(
        provider=provider,
        provider_key="stable-index-key",
    )

    await rail._install_runtime_circuit_breaker(manager)

    assert manager.provider is provider
    assert manager.provider_key == "stable-index-key"
    assert rail._embedding_health.available is False
    assert rail._embedding_health.error == "embedding validation failed: RuntimeError"
    provider.error = None
    provider.result = [0.1, 0.2]
    with pytest.raises(interface_deep._MemoryEmbeddingUnavailable):
        await provider.embed_query("query")
    assert provider.calls == 1

    clock[0] += interface_deep._MEMORY_EMBEDDING_RETRY_COOLDOWN_SECONDS

    assert await provider.embed_query("query") == [0.1, 0.2]
    assert provider.calls == 2
    assert rail._embedding_health.available is True
    assert rail._embedding_health.error is None


@pytest.mark.asyncio
async def test_memory_embedding_probe_marks_working_model_as_available() -> None:
    rail = object.__new__(interface_deep._EmbeddingHealthMemoryRail)
    rail._embedding_health = interface_deep._MemoryEmbeddingHealth()
    provider = _EmbeddingProvider(result=[0.1, 0.2])
    manager = SimpleNamespace(
        provider=provider,
        provider_key="stable-index-key",
    )

    await rail._install_runtime_circuit_breaker(manager)

    assert manager.provider is provider
    assert rail._embedding_health.available is True
    assert rail._embedding_health.error is None
    assert await provider.embed_query("query") == [0.1, 0.2]


@pytest.mark.asyncio
async def test_memory_runtime_failure_recovers_without_changing_index_identity(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(interface_deep, "_memory_embedding_now", lambda: clock[0])
    rail = object.__new__(interface_deep._EmbeddingHealthMemoryRail)
    rail._embedding_health = interface_deep._MemoryEmbeddingHealth()
    provider = _EmbeddingProvider(result=[0.1, 0.2])
    manager = SimpleNamespace(
        provider=provider,
        provider_key="stable-index-key",
    )

    await rail._install_runtime_circuit_breaker(manager)
    provider.error = RuntimeError("service unavailable")

    with pytest.raises(RuntimeError, match="service unavailable"):
        await provider.embed_query("query")

    assert manager.provider is provider
    assert manager.provider_key == "stable-index-key"
    assert rail._embedding_health.available is False
    assert rail._embedding_health.error == "embedding request failed: RuntimeError"
    provider.error = None
    with pytest.raises(interface_deep._MemoryEmbeddingUnavailable):
        await provider.embed_query("query")
    assert provider.calls == 2

    clock[0] += interface_deep._MEMORY_EMBEDDING_RETRY_COOLDOWN_SECONDS

    assert await provider.embed_query("query") == [0.1, 0.2]
    assert provider.calls == 3
    assert manager.provider_key == "stable-index-key"
    assert rail._embedding_health.available is True
    assert rail._embedding_health.error is None


@pytest.mark.asyncio
async def test_memory_half_open_allows_only_one_recovery_probe(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(interface_deep, "_memory_embedding_now", lambda: clock[0])
    rail = object.__new__(interface_deep._EmbeddingHealthMemoryRail)
    rail._embedding_health = interface_deep._MemoryEmbeddingHealth()
    provider = _EmbeddingProvider(error=RuntimeError("service unavailable"))
    manager = SimpleNamespace(provider=provider, provider_key="stable-index-key")
    await rail._install_runtime_circuit_breaker(manager)

    provider.error = None
    provider.result = [0.1, 0.2]
    provider.started = asyncio.Event()
    provider.release = asyncio.Event()
    clock[0] += interface_deep._MEMORY_EMBEDDING_RETRY_COOLDOWN_SECONDS

    recovery = asyncio.create_task(provider.embed_query("first query"))
    await provider.started.wait()
    with pytest.raises(interface_deep._MemoryEmbeddingUnavailable):
        await provider.embed_query("second query")

    provider.release.set()
    assert await recovery == [0.1, 0.2]
    assert provider.calls == 2
    assert rail._embedding_health.available is True


@pytest.mark.asyncio
async def test_memory_search_tool_exposes_keyword_fallback_state(monkeypatch) -> None:
    tool = object.__new__(interface_deep._MemorySearchToolWithHealth)
    tool._ctx = SimpleNamespace(manager=SimpleNamespace())
    tool._health = interface_deep._MemoryEmbeddingHealth(
        available=False,
        error="embedding validation failed: RuntimeError",
    )
    monkeypatch.setattr(
        interface_deep.memory_tool_ops,
        "memory_search_with_context",
        AsyncMock(return_value={"results": [{"path": "MEMORY.md"}], "disabled": False}),
    )

    output = await tool.invoke({"query": "Orion"})

    assert output.success is True
    assert output.data["search_mode"] == "keyword_only"
    assert output.data["embedding_available"] is False
    assert output.data["embedding_error"] == "embedding validation failed: RuntimeError"
    assert output.data["provider"] is None
    assert output.data["model"] is None


@pytest.mark.asyncio
async def test_memory_search_tool_exposes_semantic_state_from_manager(monkeypatch) -> None:
    health = interface_deep._MemoryEmbeddingHealth(available=True)
    manager = SimpleNamespace()
    setattr(manager, interface_deep._MEMORY_EMBEDDING_HEALTH_ATTR, health)
    tool = object.__new__(interface_deep._MemorySearchToolWithHealth)
    tool._ctx = SimpleNamespace(manager=manager)
    tool._health = interface_deep._MemoryEmbeddingHealth()
    monkeypatch.setattr(
        interface_deep.memory_tool_ops,
        "memory_search_with_context",
        AsyncMock(
            return_value={
                "results": [{"path": "MEMORY.md"}],
                "provider": "openai_compatible",
                "model": "BAAI/bge-m3",
                "disabled": False,
            }
        ),
    )

    output = await tool.invoke({"query": "Orion"})

    assert output.success is True
    assert output.data["search_mode"] == "semantic"
    assert output.data["embedding_available"] is True
    assert output.data["embedding_error"] is None
    assert output.data["provider"] == "openai_compatible"
    assert output.data["model"] == "BAAI/bge-m3"


@pytest.mark.asyncio
async def test_memory_manager_contract_falls_back_to_keyword_on_embedding_error() -> None:
    rail = interface_deep._EmbeddingHealthMemoryRail(
        embedding_config=EmbeddingConfig(
            model_name="test-model",
            base_url="https://embedding.invalid",
            api_key="test-key",
        )
    )
    provider = _EmbeddingProvider(error=RuntimeError("service unavailable"))
    manager = object.__new__(MemoryIndexManager)
    manager.provider = provider
    manager.provider_key = "stable-index-key"
    manager.settings = SimpleNamespace(
        sync={"onSearch": False},
        query={
            "max_results": 10,
            "min_score": 0.7,
            "keywordMinScore": 0.1,
            "hybrid": {"enabled": True, "candidateMultiplier": 2.0},
        },
    )
    manager.dirty = False
    manager.fts_available = True
    keyword_result = {
        "path": "MEMORY.md",
        "start_line": 1,
        "end_line": 1,
        "score": 0.5,
    }
    manager._search_keyword = AsyncMock(return_value=[keyword_result])
    manager._search_vector = AsyncMock(return_value=[])

    await rail._install_runtime_circuit_breaker(manager)
    results = await manager.search("Orion")

    assert results == [keyword_result]
    manager._search_keyword.assert_awaited_once()
    manager._search_vector.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_search_tool_uses_explicit_keyword_fallback(monkeypatch) -> None:
    health = interface_deep._MemoryEmbeddingHealth(
        available=False,
        error="embedding request failed: RuntimeError",
    )
    manager = SimpleNamespace(
        settings=SimpleNamespace(
            query={
                "max_results": 3,
                "keywordMinScore": 0.1,
                "hybrid": {"candidateMultiplier": 2.0},
            }
        ),
        _search_keyword=AsyncMock(
            return_value=[
                {
                    "path": "MEMORY.md",
                    "start_line": 2,
                    "end_line": 4,
                    "score": 0.5,
                }
            ]
        ),
    )
    setattr(manager, interface_deep._MEMORY_EMBEDDING_HEALTH_ATTR, health)
    ctx = SimpleNamespace(manager=manager)
    tool = interface_deep._MemorySearchToolWithHealth(ctx, health, "cn", "agent")
    monkeypatch.setattr(
        interface_deep.memory_tool_ops,
        "memory_search_with_context",
        AsyncMock(side_effect=interface_deep._MemoryEmbeddingUnavailable("unavailable")),
    )

    output = await tool.invoke({"query": "Orion"})

    assert output.success is True
    assert output.data["search_mode"] == "keyword_only"
    assert output.data["results"][0]["citation"] == "MEMORY.md#L2-L4"
    manager._search_keyword.assert_awaited_once_with("Orion", 6)


@pytest.mark.asyncio
async def test_memory_search_tool_stream_yields_invoke_result(monkeypatch) -> None:
    tool = object.__new__(interface_deep._MemorySearchToolWithHealth)
    expected = interface_deep.ToolOutput(success=True, data={"results": []})
    monkeypatch.setattr(tool, "invoke", AsyncMock(return_value=expected))

    chunks = [chunk async for chunk in tool.stream({"query": "Orion"})]

    assert chunks == [expected]


@pytest.mark.asyncio
async def test_memory_rail_initialization_installs_circuit_breaker(monkeypatch) -> None:
    provider = _EmbeddingProvider(result=[0.1, 0.2])
    manager = SimpleNamespace(provider=provider, provider_key="stable-index-key")
    rail = interface_deep._EmbeddingHealthMemoryRail(
        embedding_config=EmbeddingConfig(
            model_name="test-model",
            base_url="https://embedding.invalid",
            api_key="test-key",
        )
    )
    rail._tool_ctx = SimpleNamespace(manager=None)

    async def initialize_manager(base_rail, _ctx) -> None:
        base_rail._tool_ctx.manager = manager

    monkeypatch.setattr(
        interface_deep.MemoryRail,
        "_init_memory_manager",
        initialize_manager,
    )

    await rail._init_memory_manager(SimpleNamespace())

    assert rail._tool_ctx.manager is manager
    assert getattr(provider, interface_deep._MEMORY_EMBEDDING_WRAPPED_ATTR) is True
    assert rail._embedding_health.available is True


def test_memory_tool_registration_accepts_no_result(monkeypatch) -> None:
    from openjiuwen.core.memory.lite import config as memory_config
    from openjiuwen.core.memory.lite import memory_tool_context
    from openjiuwen.harness.tools import memory as memory_tools_module

    rail = interface_deep._EmbeddingHealthMemoryRail(
        embedding_config=EmbeddingConfig(
            model_name="test-model",
            base_url="https://embedding.invalid",
            api_key="test-key",
        )
    )
    rail.system_prompt_builder = SimpleNamespace(language="cn")
    rail.workspace = SimpleNamespace(get_node_path=lambda _name: "memory")
    rail.sys_operation = None
    native_search = SimpleNamespace(card=SimpleNamespace(name="memory_search"))
    read_memory = SimpleNamespace(card=SimpleNamespace(name="read_memory"))
    tool_ctx = SimpleNamespace(manager=None)
    monkeypatch.setattr(memory_config, "create_memory_settings", lambda _path: object())
    monkeypatch.setattr(memory_tool_context, "MemoryToolContext", lambda **_kwargs: tool_ctx)
    monkeypatch.setattr(
        memory_tools_module,
        "create_memory_tools",
        lambda *_args, **_kwargs: [native_search, read_memory],
    )

    class AbilityManager:
        def __init__(self) -> None:
            self.added = []

        def add_ability(self, card, tool):
            self.added.append((card, tool))
            return None

    ability_manager = AbilityManager()
    agent = SimpleNamespace(
        ability_manager=ability_manager,
        card=SimpleNamespace(id="agent"),
    )

    rail._register_memory_tools(agent)

    assert [card.name for card, _tool in ability_manager.added] == [
        "memory_search",
        "read_memory",
    ]
    assert set(rail._owned_tool_cards) == {"memory_search", "read_memory"}


def test_memory_tool_registration_continues_after_one_failure(monkeypatch) -> None:
    from openjiuwen.core.memory.lite import config as memory_config
    from openjiuwen.core.memory.lite import memory_tool_context
    from openjiuwen.harness.tools import memory as memory_tools_module

    rail = interface_deep._EmbeddingHealthMemoryRail(
        embedding_config=EmbeddingConfig(
            model_name="test-model",
            base_url="https://embedding.invalid",
            api_key="test-key",
        )
    )
    rail.system_prompt_builder = SimpleNamespace(language="cn")
    rail.workspace = SimpleNamespace(get_node_path=lambda _name: "memory")
    rail.sys_operation = None
    native_search = SimpleNamespace(card=SimpleNamespace(name="memory_search"))
    read_memory = SimpleNamespace(card=SimpleNamespace(name="read_memory"))
    monkeypatch.setattr(memory_config, "create_memory_settings", lambda _path: object())
    monkeypatch.setattr(
        memory_tool_context,
        "MemoryToolContext",
        lambda **_kwargs: SimpleNamespace(manager=None),
    )
    monkeypatch.setattr(
        memory_tools_module,
        "create_memory_tools",
        lambda *_args, **_kwargs: [native_search, read_memory],
    )

    class AbilityManager:
        def __init__(self) -> None:
            self.attempted = []

        def add_ability(self, card, _tool):
            self.attempted.append(card.name)
            if card.name == "memory_search":
                raise RuntimeError("registration failed")
            return None

    ability_manager = AbilityManager()
    agent = SimpleNamespace(
        ability_manager=ability_manager,
        card=SimpleNamespace(id="agent"),
    )

    rail._register_memory_tools(agent)

    assert ability_manager.attempted == ["memory_search", "read_memory"]
    assert set(rail._owned_tool_cards) == {"read_memory"}
