"""Flash wiring for the existing external-memory and user-profile lifecycle."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common.memory import config as memory_module
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)


class RecordingAgent:
    def __init__(self):
        self.rails = []
        self.unregistered = []

    async def register_rail(self, rail):
        assert rail not in self.rails
        self.rails.append(rail)

    async def unregister_rail(self, rail):
        self.rails.remove(rail)
        self.unregistered.append(rail)


@pytest.fixture
def flash_memory(monkeypatch):
    config = {
        "memory": {
            "mode": "local",
            "engine": "external",
            "external": {"provider": "officeace_cloud", "user_id": "alice"},
            "office_ace_user_profile": {
                "endpoint": "https://memory.invalid", "api_key": "test-key",
            },
        },
    }
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "pc")
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(memory_module, "get_memory_section", lambda cfg=None: (cfg or config)["memory"])
    adapter = JiuwenSwarmFlashAdapter()
    adapter._instance = RecordingAgent()
    # Isolate workspace context and built-in memory from the external gates.
    monkeypatch.setattr(adapter, "_ensure_flash_context_rail", AsyncMock())
    monkeypatch.setattr(adapter, "_handle_memory_rail_by_config", AsyncMock())
    external = SimpleNamespace(kind="external", _provider=SimpleNamespace(on_session_end=AsyncMock()))
    profile = SimpleNamespace(kind="profile")
    monkeypatch.setattr(adapter, "_build_external_memory_rail", lambda: external)
    monkeypatch.setattr(adapter, "_build_office_ace_user_profile_rail", lambda: profile)
    return adapter, config


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,provider,expected", [
    ("external", "officeace_cloud", {"external", "profile"}),
    ("both", "officeace_cloud", {"external", "profile"}),
    ("builtin", "officeace_cloud", set()),
    ("none", "officeace_cloud", set()),
    ("external", "mem0", {"external"}),
    ("external", "", set()),
])
async def test_first_flash_request_obeys_engine_and_provider(flash_memory, engine, provider, expected):
    adapter, config = flash_memory
    config["memory"]["engine"] = engine
    config["memory"]["external"]["provider"] = provider
    await adapter._update_rails_for_mode("flash")
    assert {rail.kind for rail in adapter._instance.rails} == expected
    first = list(adapter._instance.rails)
    await adapter._update_rails_for_mode("flash")
    assert adapter._instance.rails == first
    assert not adapter._instance.unregistered


@pytest.mark.asyncio
async def test_missing_profile_credentials_keeps_external_memory(flash_memory):
    adapter, config = flash_memory
    config["memory"]["office_ace_user_profile"]["api_key"] = ""
    await adapter._update_rails_for_mode("flash")
    assert {rail.kind for rail in adapter._instance.rails} == {"external"}


@pytest.mark.asyncio
async def test_flash_uses_existing_disable_and_reenable_lifecycle(flash_memory):
    adapter, config = flash_memory
    await adapter._update_rails_for_mode("flash")
    external = adapter._external_memory_rail
    config["memory"]["engine"] = "none"
    await adapter._update_rails_for_mode("flash")
    assert adapter._instance.rails == []
    external._provider.on_session_end.assert_awaited_once_with()
    assert not adapter._external_memory_rail_registered
    assert not adapter._office_ace_user_profile_rail_registered
    config["memory"]["engine"] = "external"
    await adapter._update_rails_for_mode("flash")
    assert {rail.kind for rail in adapter._instance.rails} == {"external", "profile"}
