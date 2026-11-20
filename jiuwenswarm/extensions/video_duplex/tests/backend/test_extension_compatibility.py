"""Incomplete upgrades must not register a broken full-duplex plugin."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.extensions.video_duplex import extension


@pytest.fixture
def compatible_modules(monkeypatch, caplog):
    monkeypatch.setattr(extension.logger, "handlers", [caplog.handler])
    backend = "jiuwenswarm.extensions.video_duplex.backend"
    modules = {
        f"{backend}.realtime_context": SimpleNamespace(
            RealtimeContextRestorer=Mock(), ContextRestoreExpired=RuntimeError,
        ),
        f"{backend}.error_details": SimpleNamespace(duplex_failure=Mock()),
        f"{backend}.joyai_action_helper": SimpleNamespace(select_model=Mock()),
        "agent_gateway.extensions.video_duplex.backend.tasks.bridge": SimpleNamespace(GatewayTaskEndpoint=Mock()),
        "e2a.message": SimpleNamespace(ReqMethod=SimpleNamespace(VOICE_TASK_CHECKPOINT_ACK="ack")),
        f"{backend}.qwen_omni_tools": SimpleNamespace(qwen_omni_tools=lambda queue_manager=False: []),
        f"{backend}.video_live": SimpleNamespace(video_duplex_enabled=lambda: True),
    }

    def load(name):
        if name not in modules:
            raise ModuleNotFoundError(name)
        return modules[name]

    monkeypatch.setattr(extension.importlib, "import_module", load)
    return modules


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [
    "jiuwenswarm.extensions.video_duplex.backend.realtime_context",
    "jiuwenswarm.extensions.video_duplex.backend.joyai_action_helper",
    "agent_gateway.extensions.video_duplex.backend.tasks.bridge",
])
async def test_missing_dependency_skips_registration(compatible_modules, missing, caplog):
    del compatible_modules[missing]
    registry = Mock()
    assert await extension.register_extensions(registry) == []
    registry.register_application_plugin.assert_not_called()
    assert missing in caplog.text
    assert "restart Gateway" in caplog.text


@pytest.mark.asyncio
async def test_old_tool_signature_skips_registration(compatible_modules, caplog):
    def legacy_tools():
        return []

    compatible_modules["jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools"].qwen_omni_tools = legacy_tools
    registry = Mock()
    assert await extension.register_extensions(registry) == []
    registry.register_application_plugin.assert_not_called()
    assert "queue_manager" in caplog.text


@pytest.mark.asyncio
async def test_missing_protocol_capability_skips_registration(compatible_modules, caplog):
    compatible_modules["e2a.message"].ReqMethod = SimpleNamespace()
    registry = Mock()
    assert await extension.register_extensions(registry) == []
    registry.register_application_plugin.assert_not_called()
    assert "VOICE_TASK_CHECKPOINT_ACK" in caplog.text


@pytest.mark.asyncio
async def test_compatible_dependencies_register_enabled_plugin(compatible_modules):
    registry = Mock()
    plugins = await extension.register_extensions(registry)
    assert len(plugins) == 1
    assert plugins[0].is_enabled()
    registry.register_application_plugin.assert_called_once_with(plugins[0])
