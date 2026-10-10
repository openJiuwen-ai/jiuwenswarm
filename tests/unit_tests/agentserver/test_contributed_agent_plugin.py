"""An extension can mount an agent plugin without host package imports."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.runtime.agent_adapter.interface import _suppress_final_record
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn


@pytest.mark.asyncio
async def test_agent_plugin_mount_and_reload_skills(monkeypatch):
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    monkeypatch.setattr(ExtensionRegistry, "current_instance", lambda: registry)
    loaded = []

    async def load_plugin_spec(spec):
        loaded.append(spec)
        return SimpleNamespace(refs=[SimpleNamespace(kind=SimpleNamespace(value="skill"), identity="/skills/new")])

    agent = SimpleNamespace(load_plugin_spec=load_plugin_spec)
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = agent
    adapter._skill_rail = SimpleNamespace(skills=[SimpleNamespace(name="existing")])
    adapter._runtime_cron_tool_context = object()

    async def mount(services):
        assert services.agent is agent
        spec = SimpleNamespace(
            skills=[SimpleNamespace(dir="/skills/existing"), SimpleNamespace(dir="/skills/new")],
            rails=[object()],
            tools=[],
        )
        return await services.load_plugin_spec(spec)

    registry.register_agent_plugin(mount)
    await adapter._load_agent_plugins()

    assert [Path(skill.dir).name for skill in loaded[0].skills] == ["new"]
    config = SimpleNamespace(skills=["/skills/other"])
    adapter._keep_agent_plugin_skills(config)
    assert config.skills == ["/skills", "/skills/other"]


def test_extension_turn_fields_cannot_replace_host_fields(monkeypatch):
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    monkeypatch.setattr(ExtensionRegistry, "current_instance", lambda: registry)
    turn = UserTurn(text="hello", channel="web", language="en", files={})
    registry.register_turn_envelope_fields(lambda _channel, _metadata: {"origin_kind": "spoofed"})

    with pytest.raises(ValueError, match="cannot replace"):
        turn.render()


def test_extension_final_filter_is_opt_in(monkeypatch):
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    monkeypatch.setattr(ExtensionRegistry, "current_instance", lambda: registry)
    request = SimpleNamespace(metadata={"allow_silence": True})
    assert not _suppress_final_record(request, "SILENT")

    registry.register_final_filter(
        lambda req, content: req.metadata.get("allow_silence") and content == "SILENT"
    )
    assert _suppress_final_record(request, "SILENT")
    assert not _suppress_final_record(request, "An answer")
