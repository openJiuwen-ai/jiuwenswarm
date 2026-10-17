# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MoA configuration round trips through the shared Web/AgentServer handlers."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import jiuwenswarm.common.config as config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers


@pytest.fixture
def panel_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "models": {
            "defaults": [
                {"model_id": "mdl_a", "model_client_config": {"model_name": "model-a"}},
                {"model_id": "mdl_b", "model_client_config": {"model_name": "model-b"}},
            ],
            "groups": [{"model_group_id": "mgp_team", "routes": [
                {"route_id": "rte_a", "model_id": "mdl_a"},
                {"route_id": "rte_b", "model_id": "mdl_b"},
            ]}],
        },
        "unrelated": {"keep": "value"},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(handlers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    return path


@pytest.fixture
def fields():
    return {
        "multi_model_collaboration_enabled": "true",
        "moa_jev_enabled": "false",
        "moa_verifier_interval": "3",
        "moa_jev_api_base": "https://api.typesafe.ai",
        "moa_jev_model_name": "jev-latest",
    }


@pytest.mark.asyncio
async def test_save_all_round_trips_with_ui_reload(panel_config, fields):
    channel = SimpleNamespace(send_response=AsyncMock())
    reload = AsyncMock()
    await handlers.config_save_all_handler(
        channel, None, "save", {"config": fields}, None, on_config_saved=reload,
    )
    saved = channel.send_response.call_args.kwargs
    assert saved["ok"] is True
    assert set(saved["payload"]["updated"]) == set(fields)
    assert saved["payload"]["applied_without_restart"] is True
    reload.assert_awaited_once()
    raw = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
    assert raw["experimental"]["multi_model_collaboration"]["verifier_interval"] == 3
    assert raw["unrelated"] == {"keep": "value"}

    await handlers.config_get_handler(channel, None, "get", {}, None)
    public = channel.send_response.call_args.kwargs["payload"]
    assert {key: public[key] for key in fields} == fields
    assert public["moa_jev_api_key"] == ""
    assert public["moa_jev_api_key_configured"] == "false"


@pytest.mark.asyncio
async def test_jev_secret_is_encrypted_write_only_and_preserved(panel_config, fields, monkeypatch):
    crypto = SimpleNamespace(
        encrypt=lambda value: f"encrypted:{value}",
        decrypt=lambda value: value,
    )
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: crypto)
    fields.update({"moa_jev_enabled": "true", "moa_jev_api_key": "test-write-only-secret"})
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(channel, None, "save", {"config": fields}, None)
    assert channel.send_response.call_args.kwargs["ok"] is True
    assert "test-write-only-secret" not in str(channel.send_response.call_args.kwargs)
    assert yaml.safe_load(panel_config.read_text())["experimental"]["multi_model_collaboration"]["jev"]["api_key"] == "encrypted:test-write-only-secret"

    await handlers.config_get_handler(channel, None, "get", {}, None)
    public = channel.send_response.call_args.kwargs["payload"]
    assert public["moa_jev_api_key"] == ""
    assert public["moa_jev_api_key_configured"] == "true"
    assert "test-write-only-secret" not in str(public)

    fields["moa_jev_api_key"] = ""
    fields["moa_verifier_interval"] = "1"
    await handlers.config_save_all_handler(channel, None, "save", {"config": fields}, None)
    raw = yaml.safe_load(panel_config.read_text())
    assert raw["experimental"]["multi_model_collaboration"]["jev"]["api_key"] == "encrypted:test-write-only-secret"
    assert raw["experimental"]["multi_model_collaboration"]["verifier_interval"] == 1


@pytest.mark.asyncio
async def test_invalid_moa_rejects_before_any_config_writes(panel_config, fields, monkeypatch):
    before = panel_config.read_text()
    monkeypatch.setenv("ASR_MODEL_NAME", "unchanged")
    fields.update({"moa_verifier_interval": "0", "asr_model": "should-not-save"})
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(channel, None, "save", {"config": fields}, None)
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"
    assert panel_config.read_text() == before
    assert not handlers.ENV_FILE.exists()
    assert os.environ["ASR_MODEL_NAME"] == "unchanged"


@pytest.mark.asyncio
async def test_write_failures_are_reported_without_secret(panel_config, fields, monkeypatch, caplog):
    fields.update({"moa_jev_enabled": "true", "moa_jev_api_key": "test-write-only-secret"})
    before = panel_config.read_text()

    def fail(*args, **kwargs):
        raise OSError("provider failed with test-write-only-secret")

    monkeypatch.setattr(handlers, "save_moa_config_from_frontend", fail)
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_save_all_handler(channel, None, "save", {"config": fields}, None)
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is False
    assert response["error"] == "failed to save MoA configuration"
    assert "test-write-only-secret" not in str(response)
    assert "test-write-only-secret" not in caplog.text
    assert panel_config.read_text() == before


def test_moa_fields_do_not_add_execution_reload_scopes():
    assert handlers.ConfigChangeSet({}, ["moa_verifier_interval"], force=True).reload_scopes == {"web_ui"}
    assert handlers.ConfigChangeSet({}, ["moa_verifier_interval", "rsi_enabled"]).reload_scopes == {"agent_runtime", "web_ui"}


@pytest.mark.asyncio
async def test_agentserver_config_adapter_uses_same_moa_persistence(panel_config, fields):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter

    adapter = ConfigAdapter(on_config_saved=AsyncMock())
    saved = await adapter.handle(AgentRequest(
        request_id="save-moa", channel_id="web", session_id="session",
        req_method=ReqMethod.CONFIG_SAVE_ALL, params={"config": fields}, user_id="test-user",
    ))
    assert saved.ok is True
    public = await adapter.handle(AgentRequest(
        request_id="get-moa", channel_id="web", session_id="session",
        req_method=ReqMethod.CONFIG_GET, params={}, user_id="test-user",
    ))
    assert public.ok is True
    assert public.payload["multi_model_collaboration_enabled"] == "true"
    assert public.payload["moa_verifier_interval"] == "3"
    adapter._on_config_saved.assert_awaited_once()


def test_transport_log_formatter_redacts_jev_secret():
    from jiuwenswarm.gateway.routing.agent_client import _to_json

    payload = {"params": {"config": {"moa_jev_api_key": "test-write-only-secret"}}}
    formatted = _to_json(payload)
    assert "test-write-only-secret" not in formatted
    assert "****" in formatted
    assert payload["params"]["config"]["moa_jev_api_key"] == "test-write-only-secret"
