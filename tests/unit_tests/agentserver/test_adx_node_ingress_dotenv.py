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
