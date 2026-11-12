# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TTSE settings persist through Runtime writes and Front config reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control import config_service


@pytest.fixture
def panel_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("react:\n  ttse:\n    enabled: false\n    evolve_enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(config_service, "get_config_file", lambda: path)
    monkeypatch.setattr(config_service, "get_env_file", lambda: tmp_path / ".env")
    original = handlers.provider
    a2ui = SimpleNamespace(get_a2ui_config_payload=lambda raw: {}, get_default_a2ui_config_payload=lambda: {})
    monkeypatch.setattr(handlers, "provider", lambda domain: a2ui if domain == "a2ui" else original(domain))
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_ttse_switch_round_trips_from_runtime_save_to_front_read(panel_config, enabled):
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(
        channel, None, "save", {"config": {"ttse_enabled": str(enabled).lower()}}, None,
    )
    assert channel.send_response.call_args.kwargs["ok"] is True
    saved = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
    assert saved["react"]["ttse"] == {"enabled": enabled, "evolve_enabled": False}

    await handlers.config_get_handler(channel, None, "runtime-read", {}, None)
    runtime_payload = channel.send_response.call_args.kwargs["payload"]
    response = await config_service.handle_config_request(AgentRequest(
        request_id="front-read", channel_id="web", req_method=ReqMethod.CONFIG_GET, params={},
    ))
    assert response.ok is True
    assert response.payload["ttse_enabled"] == runtime_payload["ttse_enabled"] == str(enabled).lower()


@pytest.mark.parametrize("content", ["react: {}\n", "react: [invalid]\n"])
def test_ttse_switch_front_read_defaults_to_false(panel_config, content):
    panel_config.write_text(content, encoding="utf-8")
    assert config_service.get_panel()["ttse_enabled"] == "false"
