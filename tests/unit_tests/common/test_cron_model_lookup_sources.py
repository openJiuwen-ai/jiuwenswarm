# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cron 模型校验在 AgentOS 运行时必须能看到 models.agentos 中的可用模型。

AgentOS 部署会把对话模型放在 ``models.agentos``（``models.defaults`` 只剩下
``${MODEL_NAME}`` 占位符）。cron 只查 defaults 时会误报
``Unknown model ... (no models configured)``。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common import config as config_mod
from jiuwenswarm.common.config import get_model_config
from jiuwenswarm.runtime.cron.models import validate_cron_model


def _entry(model_name: str, **extra) -> dict:
    return {
        "model_client_config": {
            "model_name": model_name,
            "api_base": "https://example.com/v1",
            "api_key": "sk-test",
            "client_provider": "OpenAI",
        },
        "model_config_obj": {},
        **extra,
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MODEL_NAME",
        "MODEL_ALIAS",
        "API_BASE",
        "API_KEY",
        "MODEL_PROVIDER",
        "JIUWENSWARM_RUNTIME_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda *args, **kwargs: [],
    )


def _patch_config(monkeypatch: pytest.MonkeyPatch, models: dict) -> None:
    monkeypatch.setattr(config_mod, "get_config_raw", lambda: {"models": models})


def test_agentos_model_found_when_defaults_are_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复现 lfq 的配置：defaults 是空占位，真实模型在 agentos。"""
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    _patch_config(
        monkeypatch,
        {"defaults": [_entry("${MODEL_NAME}")], "agentos": [_entry("GLM-5.2")]},
    )

    entry = get_model_config("GLM-5.2")
    assert entry is not None
    assert entry["model_client_config"]["model_name"] == "GLM-5.2"
    assert validate_cron_model("GLM-5.2") == "GLM-5.2"


def test_agentos_fallback_is_disabled_outside_agentos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 AgentOS 部署里 models.agentos 只是只读备份，不参与查找。"""
    _patch_config(
        monkeypatch,
        {"defaults": [_entry("${MODEL_NAME}")], "agentos": [_entry("GLM-5.2")]},
    )

    assert get_model_config("GLM-5.2") is None
