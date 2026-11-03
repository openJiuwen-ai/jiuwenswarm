"""Config saves in AgentServer publish the runtime snapshot before success."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import (
    ConfigAdapter,
    reload_runtime_after_config_save,
)
from tests.unit_tests.server.test_gateway_adapter import _request


@pytest.mark.asyncio
async def test_config_adapter_awaits_save_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_register(channel, **kwargs):
        seen["on_config_saved"] = kwargs.get("on_config_saved")

        async def _config_set(ws, req_id, params, session_id):
            _ = (ws, params, session_id)
            applied = True
            callback = kwargs.get("on_config_saved")
            if callback is not None:
                applied = bool(await callback(
                    {"permissions_enabled"},
                    env_updates={},
                    config_payload={"permissions": {"enabled": False}},
                    reload_options={"reload_scopes": ["permissions"], "target_channel_id": "web"},
                ))
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"updated": ["permissions_enabled"], "applied_without_restart": applied},
            )

        channel.register_method("config.set", _config_set)

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.register_config_set_handlers",
        fake_register,
    )
    callback = AsyncMock(return_value=False)
    response = await ConfigAdapter(on_config_saved=callback).handle(
        _request(ReqMethod.CONFIG_SET, {"permissions_enabled": "false"})
    )

    assert seen["on_config_saved"] is callback
    callback.assert_awaited_once()
    assert response.payload["applied_without_restart"] is False


@pytest.mark.asyncio
async def test_permission_save_reloads_every_channel() -> None:
    manager = SimpleNamespace(reload_agents_config=AsyncMock())

    applied = await reload_runtime_after_config_save(
        manager,
        {"permissions_enabled"},
        env_updates={},
        config_payload={"permissions": {"enabled": False}},
        reload_options={"reload_scopes": ["permissions"], "target_channel_id": "web"},
    )

    assert applied is True
    manager.reload_agents_config.assert_awaited_once_with(
        {"permissions": {"enabled": False}},
        {},
        target_channel_id=None,
        reload_scopes={"permissions"},
    )


@pytest.mark.asyncio
async def test_non_permission_save_keeps_target_channel() -> None:
    manager = SimpleNamespace(reload_agents_config=AsyncMock())

    await reload_runtime_after_config_save(
        manager,
        {"models.defaults"},
        env_updates={"MODEL_NAME": "m1"},
        config_payload={"models": {}},
        reload_options={"reload_scopes": ["model"], "target_channel_id": "web"},
    )

    manager.reload_agents_config.assert_awaited_once_with(
        {"models": {}},
        {"MODEL_NAME": "m1"},
        target_channel_id="web",
        reload_scopes={"model"},
    )
