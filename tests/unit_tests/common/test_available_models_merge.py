# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import pytest

from jiuwenswarm.common import config as config_mod
from jiuwenswarm.common.auth.model_catalog import LOGIN_MODEL_SOURCE
from jiuwenswarm.common.config import get_available_models


def _configured(model_name: str, api_key: str = "user-key") -> dict:
    return {
        "model_client_config": {
            "model_name": model_name,
            "api_base": "https://user.example.com/v1",
            "api_key": api_key,
            "client_provider": "OpenAI",
        },
        "model_config_obj": {"temperature": 0.7},
    }


def _login(model_name: str) -> dict:
    return {
        "model_client_config": {
            "model_name": model_name,
            "api_base": "https://api.modelarts-maas.com/v2",
            "api_key": "huawei-maas-session",
            "client_provider": "OpenAI",
            "custom_headers": {"Authorization": "Basic x"},
        },
        "model_config_obj": {"temperature": 0.95},
        "source": LOGIN_MODEL_SOURCE,
        "read_only": True,
    }


@pytest.fixture
def patched(monkeypatch):
    def _apply(configured: list[dict], login: list[dict]):
        monkeypatch.setattr(config_mod, "get_default_models", lambda _c=None: list(configured))
        monkeypatch.setattr(
            "jiuwenswarm.common.auth.model_catalog.list_login_model_entries",
            lambda _s=None: list(login),
        )

    return _apply


def test_without_a_session_there_are_no_login_models(patched):
    patched([_configured("my-gpt")], [_login("glm-5")])
    assert [e["model_client_config"]["model_name"] for e in get_available_models(None)] == ["my-gpt"]


def test_login_models_are_appended(patched):
    patched([_configured("my-gpt")], [_login("glm-5")])
    names = [e["model_client_config"]["model_name"] for e in get_available_models(None, "sess-1")]
    assert names == ["my-gpt", "glm-5"]


def test_configured_wins_on_name_collision(patched):
    patched([_configured("glm-5")], [_login("glm-5")])
    models = get_available_models(None, "sess-1")
    assert len(models) == 1
    assert models[0]["model_client_config"]["api_key"] == "user-key"


def test_no_login_models_is_identical_to_configured(patched):
    patched([_configured("a"), _configured("b")], [])
    assert [e["model_client_config"]["model_name"] for e in get_available_models(None, "sess-1")] == ["a", "b"]


def test_login_module_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(config_mod, "get_default_models", lambda _c=None: [_configured("a")])

    def _boom(_session_id=None):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.list_login_model_entries", _boom
    )
    assert [e["model_client_config"]["model_name"] for e in get_available_models(None, "sess-1")] == ["a"]


def test_login_entries_carry_marker(patched):
    patched([], [_login("glm-5")])
    entry = get_available_models(None, "sess-1")[0]
    assert entry["source"] == LOGIN_MODEL_SOURCE
    assert entry["read_only"] is True


def test_case_differing_names_are_kept_as_separate_models(patched):
    patched([_configured("GLM-5.2")], [_login("glm-5.2")])
    names = [e["model_client_config"]["model_name"] for e in get_available_models(None, "sess-1")]
    assert names == ["GLM-5.2", "glm-5.2"]


def test_login_models_always_come_after_configured_ones(patched):
    patched([_configured("my-gpt"), _configured("my-qwen")], [_login("kimi-k2.6"), _login("glm-5.2")])
    names = [e["model_client_config"]["model_name"] for e in get_available_models(None, "sess-1")]
    assert names == ["my-gpt", "my-qwen", "kimi-k2.6", "glm-5.2"]
