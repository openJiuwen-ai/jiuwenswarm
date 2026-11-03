# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Jev clearing through the existing browser and batch configuration methods."""

import os
from pathlib import Path
from unittest.mock import AsyncMock

from dotenv import dotenv_values
import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.browser_settings import JEV_PROVIDER_DEFAULTS, resolve_jev_connection
from jiuwenswarm.common.config_panel import config_set_handlers as handlers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(JEV_PROVIDER_DEFAULTS))
async def test_clear_jev_retains_connection_and_other_credentials(tmp_path, monkeypatch, provider):
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    credentials = {preset["api_key_env"]: "synthetic-key" for preset in JEV_PROVIDER_DEFAULTS.values()}
    credentials["API_KEY"] = "synthetic-primary-key"
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    env_path.write_text("".join(f"{name}={value}\n" for name, value in credentials.items()), encoding="utf-8")
    original = {
        "browser": {"chrome_path": "/custom/chrome", "headless": False, "decision": {
            **JEV_PROVIDER_DEFAULTS[provider], "provider": provider, "mode": "hybrid",
            "api_base": "https://jev.example.test/v1", "display_name": "Old Jev",
            "min_confidence": 0.55, "request_timeout_ms": 4500,
        }},
        "experimental": {"multi_model_collaboration_enabled": True,
                         "multi_model_collaboration": {"jev_enabled": True, "verifier_interval": 5}},
    }
    config_path.write_text(yaml.safe_dump(original), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config_module, "get_config_file", lambda: config_path)
    monkeypatch.setattr(handlers, "ENV_FILE", env_path)
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    callback = AsyncMock(return_value=True)
    adapter = ConfigAdapter(on_config_saved=callback)

    disabled = await adapter.handle(AgentRequest(
        request_id="clear-browser", channel_id="web", session_id="session", req_method=ReqMethod.PATH_SET,
        params={"chrome_path": "/custom/chrome", "headless": False,
                "decision_mode": "llm", "decision_display_name": ""},
    ))
    assert disabled.ok is True
    assert disabled.payload["decision_display_name"] == ""
    assert disabled.metadata["browser_runtime_restart"] is True

    key = JEV_PROVIDER_DEFAULTS[provider]["api_key_env"]
    cleared = await adapter.handle(AgentRequest(
        request_id="clear-key", channel_id="web", session_id="session", req_method=ReqMethod.CONFIG_SAVE_ALL,
        params={"config": {f"jev_{provider}_api_key": "", "moa_jev_enabled": "false"}},
    ))
    assert cleared.ok is True
    callback.assert_awaited_once()
    assert callback.call_args.kwargs["env_updates"] == {key: ""}
    expected_credentials = {**credentials, key: ""}
    assert dotenv_values(env_path) == expected_credentials
    assert {name: os.environ[name] for name in credentials} == expected_credentials
    expected_decision = {**original["browser"]["decision"], "mode": "llm", "display_name": ""}
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["browser"] == {**original["browser"], "decision": expected_decision}
    assert persisted["experimental"] == {
        "multi_model_collaboration_enabled": True,
        "multi_model_collaboration": {"jev_enabled": False, "verifier_interval": 5},
    }
    restored = await adapter.handle(AgentRequest(
        request_id="read-browser", channel_id="web", session_id="session", req_method=ReqMethod.PATH_GET, params={},
    ))
    assert restored.payload["jev_key_configured"] is False
    assert restored.payload["decision_display_name"] == ""
    with pytest.raises(ValueError, match="Jev API key"):
        resolve_jev_connection(persisted)

    template = tmp_path / "template.yaml"
    resource = Path(__file__).parents[3] / "jiuwenswarm/resources/config.yaml"
    template.write_text(yaml.safe_dump({"browser": yaml.safe_load(resource.read_text())["browser"]}), encoding="utf-8")
    config_module.migrate_config_from_template(template, config_path)
    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for name, value in expected_decision.items():
        assert migrated["browser"]["decision"][name] == value


def test_startup_defaults_keep_existing_model_and_empty_display_name(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"browser": {"decision": {
        "provider": "custom", "model": "private-jev-model", "api_base": "https://private.example.test/v1",
    }}}), encoding="utf-8")
    resource = Path(__file__).parents[3] / "jiuwenswarm/resources/config.yaml"
    config_module.migrate_config_from_template(resource, config_path)
    decision = yaml.safe_load(config_path.read_text(encoding="utf-8"))["browser"]["decision"]
    assert decision["model"] == "private-jev-model"
    assert decision["display_name"] == ""
