# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MoA settings persist through Runtime writes and Front config reads."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import jiuwenswarm.common.config as config
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control import config_service


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
    monkeypatch.setattr(config_service, "get_config_file", lambda: path)
    monkeypatch.setattr(config_service, "get_env_file", lambda: tmp_path / ".env")
    a2ui = SimpleNamespace(
        get_a2ui_config_payload=lambda raw: {},
        get_default_a2ui_config_payload=lambda: {},
    )
    original_provider = handlers.provider
    monkeypatch.setattr(handlers, "provider", lambda domain: a2ui if domain == "a2ui" else original_provider(domain))
    return path


@pytest.fixture
def fields():
    return {
        "multi_model_collaboration_enabled": "true",
        "moa_jev_enabled": "false",
        "moa_verifier_interval": "3",
    }


@pytest.mark.parametrize("interval,expected_interval", [(None, "3"), (5, "5")])
@pytest.mark.parametrize("through_adapter", [False, True], ids=["panel", "agentserver"])
@pytest.mark.asyncio
async def test_config_get_moa_interval_preserves_configuration(panel_config, interval, expected_interval, through_adapter):
    if interval is not None:
        raw = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
        raw["experimental"] = {"multi_model_collaboration": {"verifier_interval": interval}}
        panel_config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = panel_config.read_bytes()

    if through_adapter:
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter

        on_config_saved = AsyncMock()
        adapter = ConfigAdapter(on_config_saved=on_config_saved)
        response = await adapter.handle(AgentRequest(
            request_id="get-moa", channel_id="web", session_id="session",
            req_method=ReqMethod.CONFIG_GET, params={}, user_id="test-user",
        ))
        assert response.ok is True
        public = response.payload
        on_config_saved.assert_not_awaited()
    else:
        channel = SimpleNamespace(send_response=AsyncMock())
        await handlers.config_get_handler(channel, None, "get", {}, None)
        response = channel.send_response.call_args.kwargs
        assert response["ok"] is True
        public = response["payload"]

    assert public["moa_verifier_interval"] == expected_interval
    assert panel_config.read_bytes() == before


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
    assert "moa_jev_api_key" not in public
    assert "moa_jev_api_key_configured" not in public


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("jev_enabled", [True, False])
@pytest.mark.asyncio
async def test_moa_switches_round_trip_from_runtime_save_to_front_read(
    panel_config, monkeypatch, enabled, jev_enabled,
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-shared-key")
    channel = SimpleNamespace(send_response=AsyncMock())
    for collaboration, jev, interval in (
        (enabled, jev_enabled, 5),
        (not enabled, not jev_enabled, 3),
    ):
        fields = {
            "multi_model_collaboration_enabled": str(collaboration).lower(),
            "moa_jev_enabled": str(jev).lower(),
            "moa_verifier_interval": str(interval),
        }
        await handlers.config_save_all_handler(
            channel, None, "save", {"config": fields}, None, on_config_saved=AsyncMock(),
        )
        saved = channel.send_response.call_args.kwargs
        assert saved["ok"] is True
        assert set(saved["payload"]["updated"]) == set(fields)
        raw = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
        assert raw["experimental"] == {
            "multi_model_collaboration_enabled": collaboration,
            "multi_model_collaboration": {"jev_enabled": jev, "verifier_interval": interval},
        }
        assert raw["unrelated"] == {"keep": "value"}
        before = panel_config.read_bytes()

        response = await config_service.handle_config_request(AgentRequest(
            request_id="front-read", channel_id="web", req_method=ReqMethod.CONFIG_GET, params={},
        ))
        assert response.ok is True
        assert {key: response.payload[key] for key in fields} == fields
        assert "moa_jev_api_key" not in response.payload
        assert "moa_jev_api_key_configured" not in response.payload
        assert panel_config.read_bytes() == before


@pytest.mark.parametrize("content", ["unrelated: keep\n", "experimental: {}\n", "experimental: null\n"])
def test_front_moa_defaults_without_experimental_settings(panel_config, content):
    panel_config.write_text(content, encoding="utf-8")
    before = panel_config.read_bytes()

    public = config_service.get_panel()

    assert public["multi_model_collaboration_enabled"] == "false"
    assert public["moa_jev_enabled"] == "false"
    assert public["moa_verifier_interval"] == "3"
    assert panel_config.read_bytes() == before


def test_front_moa_defaults_when_config_read_fails(panel_config, monkeypatch):
    before = panel_config.read_bytes()

    def fail():
        raise OSError("config read failed")

    monkeypatch.setattr(config_service, "_read_raw_config", fail)

    public = config_service.get_panel()

    assert public["multi_model_collaboration_enabled"] == "false"
    assert public["moa_jev_enabled"] == "false"
    assert public["moa_verifier_interval"] == "3"
    assert panel_config.read_bytes() == before


def test_front_moa_keeps_projected_values_when_unrelated_settings_fail(panel_config, caplog):
    raw = yaml.safe_load(panel_config.read_text(encoding="utf-8"))
    raw.update({
        "experimental": {
            "multi_model_collaboration_enabled": True,
            "multi_model_collaboration": {"jev_enabled": True, "verifier_interval": 5},
        },
        "setup_guide": ["invalid"],
    })
    panel_config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = panel_config.read_bytes()

    public = config_service.get_panel()

    assert "panel yaml merge failed" in caplog.text
    assert public["multi_model_collaboration_enabled"] == "true"
    assert public["moa_jev_enabled"] == "true"
    assert public["moa_verifier_interval"] == "5"
    assert panel_config.read_bytes() == before


@pytest.mark.asyncio
async def test_shared_key_is_saved_once_and_used_by_collaboration(panel_config, fields, monkeypatch):
    crypto = SimpleNamespace(
        encrypt=lambda value: f"encrypted:{value}",
        decrypt=lambda value: value,
    )
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: crypto)
    channel = SimpleNamespace(send_response=AsyncMock())
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    await handlers.config_save_all_handler(channel, None, "key", {
        "config": {"jev_typesafe_api_key": "test-shared-secret"},
    }, None)
    assert channel.send_response.call_args.kwargs["ok"] is True
    assert os.environ["TYPESAFE_API_KEY"] == "encrypted:test-shared-secret"
    fields["moa_jev_enabled"] = "true"
    await handlers.config_save_all_handler(channel, None, "save", {"config": fields}, None)
    assert channel.send_response.call_args.kwargs["ok"] is True
    assert "test-shared-secret" not in str(channel.send_response.call_args.kwargs)
    assert "jev" not in yaml.safe_load(panel_config.read_text())["experimental"]["multi_model_collaboration"]

    await handlers.config_get_handler(channel, None, "get", {}, None)
    public = channel.send_response.call_args.kwargs["payload"]
    assert "moa_jev_api_key" not in public
    assert public["moa_jev_enabled"] == "true"

    fields["moa_verifier_interval"] = "1"
    await handlers.config_save_all_handler(channel, None, "save", {"config": fields}, None)
    raw = yaml.safe_load(panel_config.read_text())
    assert os.environ["TYPESAFE_API_KEY"] == "encrypted:test-shared-secret"
    assert raw["experimental"]["multi_model_collaboration"]["verifier_interval"] == 1
    await handlers.config_save_all_handler(channel, None, "clear", {
        "config": {"jev_typesafe_api_key": "", "moa_jev_enabled": "false"},
    }, None)
    assert channel.send_response.call_args.kwargs["ok"] is True
    assert yaml.safe_load(panel_config.read_text())["experimental"]["multi_model_collaboration"]["jev_enabled"] is False


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
    from agent_gateway.routing.agent_client import _to_json

    payload = {"params": {"config": {"moa_jev_api_key": "test-write-only-secret"}}}
    formatted = _to_json(payload)
    assert "test-write-only-secret" not in formatted
    assert "****" in formatted
    assert payload["params"]["config"]["moa_jev_api_key"] == "test-write-only-secret"


@pytest.mark.asyncio
async def test_a2ui_panel_without_registered_provider(panel_config, monkeypatch):
    original = handlers.provider

    def without_a2ui(domain):
        if domain == "a2ui":
            raise RuntimeError("A2UI provider not registered in Gateway")
        return original(domain)

    monkeypatch.setattr(handlers, "provider", without_a2ui)
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    monkeypatch.delenv("JIUWENSWARM_A2UI_PROTOCOL_VERSION", raising=False)
    channel = SimpleNamespace(send_response=AsyncMock())
    await handlers.config_get_handler(channel, None, "get", {}, None)
    assert channel.send_response.call_args.kwargs["ok"] is True
    assert channel.send_response.call_args.kwargs["payload"]["a2ui_enabled"] == "true"
