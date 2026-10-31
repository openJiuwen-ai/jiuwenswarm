"""Host-selected route applications must not fall back to the shared default."""

from pathlib import Path

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider import LocalDingTalkCliRuntime
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope


def runtime(tmp_path):
    apps = [
        {"client_id": "default-app", "client_secret": "default-secret", "is_default": True,
         "enabled": True, "allow_from": []},
        {"client_id": "agent-app", "client_secret": "agent-secret", "persistent_agent_id": "agent",
         "enabled": True, "allow_from": []},
    ]
    return LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: {"channels": {"dingtalk": {"apps": apps}}},
        dws_path=Path("/tools/dws"),
    ), apps


def test_trusted_route_app_overrides_a_different_default(tmp_path):
    provider, _ = runtime(tmp_path)
    assert provider._binding(DingTalkCliScope("local", "agent", app_id="agent-app")) == (
        "agent-app", "agent-secret",
    )
    assert provider._binding(DingTalkCliScope("local", "another-agent")) == ("default-app", "default-secret")


@pytest.mark.parametrize("change", ["missing", "disabled", "duplicate", "wrong-agent"])
def test_explicit_app_failure_never_falls_back_to_default(tmp_path, change):
    provider, apps = runtime(tmp_path)
    if change == "missing":
        apps.pop()
    elif change == "disabled":
        apps[1]["enabled"] = False
    elif change == "duplicate":
        apps.append(dict(apps[1]))
    else:
        apps[1]["persistent_agent_id"] = "another-agent"
    assert provider._binding(DingTalkCliScope("local", "agent", app_id="agent-app")) is None
