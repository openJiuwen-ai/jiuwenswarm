# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Node-local ingress is owned by the AgentOS launcher."""

import os

import pytest

from jiuwenswarm.dotenv_early import load_dotenv_runtime


@pytest.mark.parametrize(
    ("profile", "injected", "expected"),
    [
        ("agentos", "https://worker:8443", "https://worker:8443"),
        ("agentos", None, "https://master:8443"),
        ("standalone", "https://worker:8443", "https://master:8443"),
    ],
)
def test_node_ingress_precedence(monkeypatch, tmp_path, profile, injected, expected):
    key = "JIUWENSWARM_ADX_NODE_INGRESS_URL"
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", profile)
    monkeypatch.setenv(key, injected or "")
    if injected is None:
        monkeypatch.delenv(key)
    monkeypatch.setenv("MODEL_NAME", "old-model")
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{key}=https://master:8443\nMODEL_NAME=new-model\n")

    for _ in range(2):
        assert load_dotenv_runtime(dotenv, override=True)
        assert os.environ[key] == expected
        assert os.environ["MODEL_NAME"] == "new-model"


def test_agentos_shared_dotenv_cannot_replace_injected_tool_identity(monkeypatch, tmp_path):
    injected = {
        "JIUWENSWARM_RUNTIME_PROFILE": "agentos",
        "JIUWENSWARM_AGENTOS_SANDBOX_CONFIG": '{"type":"adx-environment"}',
        "JIUWENSWARM_USER_DIRECTORY": "/shared/alice",
        "JIUWENSWARM_ADX_NODE_CONTEXT_FILE": "/run/agentos/node.json",
        "JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE": "/run/agentos/workspace-config.yaml",
        "SSL_CERT_FILE": "/etc/agentos/trust.pem",
        "ADX_TOKEN": "injected-fixture-token",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MODEL_NAME", "before")
    dotenv = tmp_path / ".env"
    dotenv.write_text("".join(f"{key}=stale\n" for key in injected) + "MODEL_NAME=after\n")
    for _ in range(2):
        assert load_dotenv_runtime(dotenv, override=True)
        assert all(os.environ[key] == value for key, value in injected.items())
        assert os.environ["MODEL_NAME"] == "after"


def test_agentos_launcher_ports_win_while_cli_can_load_generated_ports(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("GATEWAY_PORT", "29001")
    monkeypatch.setenv("FRONTEND_HOST", "0.0.0.0")
    monkeypatch.delenv("FRONTEND_PORT", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("GATEWAY_PORT=19001\nFRONTEND_HOST=localhost\nFRONTEND_PORT=25173\n")
    assert load_dotenv_runtime(dotenv, override=True)
    assert os.environ["GATEWAY_PORT"] == "29001"
    assert os.environ["FRONTEND_HOST"] == "0.0.0.0"
    assert os.environ["FRONTEND_PORT"] == "25173"


def test_agentos_launch_credential_reaches_sdk_and_survives_user_dotenv(monkeypatch, tmp_path):
    from jiuwenswarm.dotenv_early import load_agentos_api_credential
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_ADX_API_TOKEN", "launch-secret")
    monkeypatch.setenv("ADX_TOKEN", "old")
    load_agentos_api_credential()
    assert "JIUWENSWARM_ADX_API_TOKEN" not in os.environ
    dotenv = tmp_path / ".env"
    dotenv.write_text("ADX_TOKEN=stale\n")
    load_dotenv_runtime(dotenv)
    assert os.environ["ADX_TOKEN"] == "launch-secret"


def test_other_backends_do_not_consume_agentos_credential(monkeypatch):
    from jiuwenswarm.dotenv_early import load_agentos_api_credential
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "standalone")
    monkeypatch.setenv("JIUWENSWARM_ADX_API_TOKEN", "unused")
    monkeypatch.delenv("ADX_TOKEN", raising=False)
    load_agentos_api_credential()
    assert "ADX_TOKEN" not in os.environ
