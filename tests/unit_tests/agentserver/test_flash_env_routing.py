# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实 config.yaml 加载到独立部署路由及 PC 显式模式的边界。"""

import pytest
import yaml

from jiuwenswarm.agents.harness.flash.skill_selection.config import SelectionSettings
from jiuwenswarm.common import config as config_module
from jiuwenswarm.common import config_provider as providers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.dotenv_early import load_dotenv_runtime
from jiuwenswarm.server.handlers import _shared


FLASH_ENV = "JIUWENSWARM_FLASH_ENABLED"
SKILL_ENV = "JIUWENSWARM_FLASH_SKILL_SELECTION_ENABLED"


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "get_config_file", lambda: config_file)
    # dotenv 会直接写 os.environ；先登记恢复动作，确保原本缺失的变量也会被清理。
    for name in (FLASH_ENV, SKILL_ENV):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    config_module.clear_config_cache()
    providers.reset_config_provider()
    yield config_file
    config_module.clear_config_cache()
    providers.reset_config_provider()


def make_request(params, channel="web", method=ReqMethod.CHAT_SEND):
    return AgentRequest(
        request_id="flash-env-test", channel_id=channel, session_id="flash-env-session",
        req_method=method, params=dict(params),
    )


def test_standalone_dotenv_reaches_yaml_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "JIUWENSWARM_FLASH_ENABLED=true\nJIUWENSWARM_FLASH_SKILL_SELECTION_ENABLED=false\n",
        encoding="utf-8",
    )
    assert load_dotenv_runtime(dotenv)
    config = config_module.get_config()
    assert config.get("flash", {}).get("enabled") is True
    assert SelectionSettings.from_config(config).enabled is False


@pytest.mark.parametrize("method", [ReqMethod.SESSION_CREATE, ReqMethod.CHAT_SEND])
@pytest.mark.parametrize("mode", [None, "agent", "agent.work.normal"])
def test_standalone_creation_and_chat_use_resolved_configuration(monkeypatch, mode, method):
    monkeypatch.setenv(FLASH_ENV, "true")
    params = {"work_mode": "work"}
    if mode is not None:
        params["mode"] = mode
    resolved = _shared.resolve_request_runtime_mode(make_request(params, method=method))
    assert resolved.manager_mode == resolved.adapter_mode == "flash"
    assert resolved.canonical_mode in {"agent", "agent.work.normal"}


@pytest.mark.parametrize("params,expected", [
    ({"mode": "agent", "work_mode": "code"}, "code"),
    ({"mode": "code.normal"}, "code"),
    ({"mode": "team", "work_mode": "work"}, "agent"),
    ({"mode": "agent.plan", "work_mode": "work"}, "agent"),
    ({"mode": "agent.work.plan", "work_mode": "work"}, "agent"),
    ({"mode": "team.work.plan", "work_mode": "work"}, "agent"),
])
def test_environment_keeps_code_team_and_plan_routes(monkeypatch, params, expected):
    monkeypatch.setenv(FLASH_ENV, "true")
    assert _shared.resolve_request_runtime_mode(make_request(params)).adapter_mode == expected


@pytest.mark.parametrize("enabled", ["true", "false"])
@pytest.mark.parametrize("mode,expected", [(None, "agent"), ("agent.plan", "agent"), ("flash", "flash")])
def test_pc_foreground_still_uses_explicit_mode(monkeypatch, enabled, mode, expected):
    monkeypatch.setenv(FLASH_ENV, enabled)
    request = make_request({"mode": mode, "work_mode": "work"}, channel="officeclaw")
    assert _shared.resolve_request_runtime_mode(request).adapter_mode == expected


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_sparse_instance_snapshot_resolves_flash_and_skill_flags(monkeypatch, enabled):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setenv(FLASH_ENV, enabled)
    monkeypatch.setenv(SKILL_ENV, enabled)
    sparse = {"react": {"max_iterations": 7}}
    resolved = interface_deep._resolve_instance_config_base(sparse)
    expected = enabled == "true"
    assert resolved.get("flash", {}).get("enabled") is expected
    assert SelectionSettings.from_config(resolved).enabled is expected
    assert sparse == {"react": {"max_iterations": 7}}


@pytest.mark.parametrize("channel", ["web", "officeclaw"])
def test_explicit_flash_is_not_disabled_by_environment(monkeypatch, channel):
    monkeypatch.setenv(FLASH_ENV, "false")
    resolved = _shared.resolve_request_runtime_mode(make_request({"mode": "flash"}, channel=channel))
    assert resolved.adapter_mode == "flash"


@pytest.mark.parametrize("flash", [False, True])
@pytest.mark.parametrize("selection", [False, True])
def test_flash_and_skill_selection_switches_are_independent(
    isolated_configuration, monkeypatch, flash, selection,
):
    monkeypatch.setenv(FLASH_ENV, str(flash).lower())
    monkeypatch.setenv(SKILL_ENV, str(selection).lower())
    raw = {"flash": {"enabled": not flash, "skill_selection": {"enabled": not selection}}}
    isolated_configuration.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = config_module.get_config()
    resolved = _shared.resolve_request_runtime_mode(make_request({"work_mode": "work"}))
    assert resolved.adapter_mode == ("flash" if flash else "agent")
    assert SelectionSettings.from_config(config).enabled is selection
