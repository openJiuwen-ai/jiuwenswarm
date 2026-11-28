# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentOS tool-sandbox mounts (projectless task workspace visibility)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder

_USER_DIR = "/home/agentos/users/alice"


@pytest.fixture()
def agentos_mount_env(monkeypatch, tmp_path):
    """Anchor the AgentOS home and workspace under a temporary directory."""
    home = tmp_path / "home"
    workspace = home / ".jiuwenswarm" / "agent" / "workspace"
    workspace.mkdir(parents=True)
    tasks = home / "Documents" / "JiuwenSwarm"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_USER_DIRECTORY", _USER_DIR)
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tasks))
    monkeypatch.setattr(sysop_builder, "_resolve_workspace_dir", lambda: workspace)
    monkeypatch.setattr(sysop_builder, "get_sandbox_endpoint", lambda: {"type": "adx-environment"})
    return tasks


def test_task_root_mount_uses_the_host_user_directory(agentos_mount_env) -> None:
    """Projectless task cwds live under ``<home>/Documents/JiuwenSwarm`` in the
    server sandbox; without this mount every tool command starts with a failing
    ``cd`` inside the tool sandbox. The target stays the server-sandbox path
    while the source points at the host user directory."""
    tasks_root = agentos_mount_env

    extra = sysop_builder._build_adx_extra_params()

    task_mount = next((m for m in extra["mounts"] if m["target"] == str(tasks_root)), None)
    assert task_mount is not None
    assert task_mount["host_path"] == str(Path(_USER_DIR) / "Documents" / "JiuwenSwarm")
    assert task_mount["options"] == ["rw"]
    # The bind source must exist before the container is created.
    assert tasks_root.is_dir()


def test_task_root_mount_requires_the_host_user_directory(
    agentos_mount_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_USER_DIRECTORY")

    with pytest.raises(ValueError, match="user directory"):
        sysop_builder._build_adx_extra_params()


def test_adx_tool_card_serializes_yaml_policy_with_the_pinned_gateway(monkeypatch, agentos_mount_env):
    source = {
        'type': 'adx-environment',
        'runtime': 'runsc',
        'network': {
            'traffic': {
                'ingress_default_action': 'deny',
                'egress_default_action': 'deny',
                'rules': [
                    {'direction': 'ingress', 'protocol': 'tcp', 'cidr': '192.0.2.0/24',
                     'environment_port_range': {'first': 8443, 'last': 8444}},
                    {'protocol': 'tcp', 'domain': 'tools.example', 'port_range': 443, 'priority': 20},
                ],
            },
            'dns': {'default_action': 'deny', 'rules': [{'action': 'allow', 'pattern': 'tools.example'}]},
        },
    }
    monkeypatch.setattr(sysop_builder, 'get_sandbox_endpoint', lambda: source)
    card = sysop_builder.create_sandbox_sysop_card('https://worker.example:8443', 'adx-environment')

    assert card is not None
    assert card.gateway_config.launcher_config.extra_params['network'] == {
        'schemaVersion': 2,
        'traffic': {
            'ingressDefaultAction': 'deny', 'egressDefaultAction': 'deny', 'mode': 'stateful',
            'rules': [
                {'action': 'allow', 'direction': 'ingress', 'protocol': 'tcp', 'priority': 100,
                 'peer': {'cidr': '192.0.2.0/24'}, 'environmentPortRange': {'first': 8443, 'last': 8444}},
                {'action': 'allow', 'direction': 'egress', 'protocol': 'tcp', 'priority': 20,
                 'peer': {'domain': 'tools.example', 'portRange': {'first': 443, 'last': 443}}},
            ],
        },
        'dns': {'defaultAction': 'deny', 'rules': [{'action': 'allow', 'pattern': 'tools.example'}]},
    }


def test_adx_tool_management_overrides_preserve_higher_retained_limits(monkeypatch, agentos_mount_env):
    monkeypatch.setattr(sysop_builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment', 'cpu': 500, 'cpu_limit': 1000, 'memory': 512,
        'mem_limit': 8192, 'idle_timeout': 600})
    monkeypatch.setenv('TOOL_SANDBOX_CPU', '2000')
    monkeypatch.setenv('TOOL_SANDBOX_MEMORY', '2048')
    monkeypatch.setenv('TOOL_SANDBOX_IDLE_TIMEOUT', '60')
    extra = sysop_builder._build_adx_extra_params()
    assert (extra['cpu'], extra['cpu_limit']) == (2000, 2000)
    assert (extra['memory'], extra['mem_limit']) == (2048, 8192)
    assert extra['idle_timeout'] == 60
