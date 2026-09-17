# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Product configuration entry points accept only supported permission modes."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers


@pytest.mark.parametrize("mode", ["auto", " AUTO ", "${ENTRY_MODE}", "${MISSING_ENTRY_MODE:-auto}"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("reader", ["config", "snapshot"])
def test_config_read_rejects_unsupported_mode_without_writing(tmp_path, monkeypatch, mode, enabled, reader):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"permissions": {"enabled": enabled, "mode": mode}}), encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setenv("ENTRY_MODE", "auto")
    monkeypatch.delenv("MISSING_ENTRY_MODE", raising=False)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    with pytest.raises(ValueError, match="Unsupported permissions.mode; expected manual"):
        if reader == "config":
            config.get_config()
        else:
            permissions_layers.read_permission_layers_locked()
    assert path.read_bytes() == original


@pytest.mark.parametrize("mode", [None, "manual"])
@pytest.mark.parametrize("enabled", [True, False])
def test_supported_config_preserves_permission_rules(tmp_path, monkeypatch, mode, enabled):
    permissions = {"enabled": enabled, "tools": {"write_file": "ask"}, "rules": []}
    if mode is not None:
        permissions["mode"] = mode
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"permissions": permissions}), encoding="utf-8")
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    assert config.get_config()["permissions"] == permissions


def test_environment_preview_reuses_resolution_without_mutating_process(monkeypatch):
    monkeypatch.setenv("ENTRY_MODE", "manual")
    raw = {"nested": ["${ENTRY_MODE:-manual}", "${EMPTY:-manual}", "${UNSET}"]}
    assert config.resolve_env_vars(raw, env={"ENTRY_MODE": "auto", "EMPTY": ""}) == {
        "nested": ["auto", "manual", ""],
    }
    assert os.environ["ENTRY_MODE"] == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["payload", "disk", "environment", "unset"])
async def test_reload_rejects_unsupported_mode_before_side_effects(monkeypatch, source):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server import agent_ws_server as server_module

    raw = {"permissions": {"enabled": True, "mode": "auto"}}
    params = {"config": raw}
    monkeypatch.setenv("ENTRY_MODE", "manual")
    if source == "disk":
        params = {}
    elif source in {"environment", "unset"}:
        raw["permissions"]["mode"] = "${ENTRY_MODE:-auto}"
        params["env"] = {"ENTRY_MODE": "auto" if source == "environment" else None}
    monkeypatch.setattr(config, "get_config_raw", lambda: raw)
    server = server_module.AgentWebSocketServer()
    reload_agents = AsyncMock()
    tokenizer = Mock()
    image = Mock()
    monkeypatch.setattr(server._agent_manager, "reload_agents_config", reload_agents)
    monkeypatch.setattr(server, "_schedule_tokenizer_warmup", tokenizer)
    monkeypatch.setattr(server, "schedule_image_modality_warmup", image)
    monkeypatch.setattr(
        server_module, "encode_agent_response_for_wire",
        lambda response, response_id: {"ok": response.ok, "payload": response.payload},
    )
    ws = Mock(send=AsyncMock())
    request = AgentRequest(
        request_id="unsupported-permission-mode", channel_id="web",
        req_method=ReqMethod.AGENT_RELOAD_CONFIG, params=params,
    )

    await server._handle_agent_reload_config(ws, request, asyncio.Lock())

    reload_agents.assert_not_called()
    tokenizer.assert_not_called()
    image.assert_not_called()
    assert os.environ["ENTRY_MODE"] == "manual"
    response = json.loads(ws.send.call_args.args[0])
    assert response["ok"] is False
    assert response["payload"]["error"] == "Unsupported permissions.mode; expected manual"
