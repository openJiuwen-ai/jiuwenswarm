# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os

import pytest

from jiuwenswarm.common.local_env_config import (
    ENV_CONFIG_DICT,
    bind_task_env_overlay,
    clear_staged_env,
    reset_task_env_overlay,
)


@pytest.fixture(autouse=True)
def _reset_env_state():
    saved = dict(os.environ)
    ENV_CONFIG_DICT.clear()
    clear_staged_env()
    yield
    ENV_CONFIG_DICT.clear()
    clear_staged_env()
    os.environ.clear()
    os.environ.update(saved)


def test_web_search_paid_availability_reads_tip_not_bare_os_environ():
    from jiuwenswarm.agents.harness.common.tools.web_search.providers import (
        paid_provider_available,
    )

    os.environ["BOCHA_API_KEY"] = "bare-os-pollution"

    token = bind_task_env_overlay({"BOCHA_API_KEY": "tip-bocha"})
    try:
        assert paid_provider_available("bocha") is True
    finally:
        reset_task_env_overlay(token)

    empty = bind_task_env_overlay({})
    try:
        assert paid_provider_available("bocha") is False
    finally:
        reset_task_env_overlay(empty)


def test_image_vision_credentials_use_tip():
    from jiuwenswarm.agents.harness.common.tools import image_tools

    os.environ["VISION_API_KEY"] = "bare-key"
    os.environ["API_KEY"] = "bare-api"

    token = bind_task_env_overlay(
        {
            "VISION_API_KEY": "tip-vision",
            "VISION_API_BASE": "https://tip.example",
            "VISION_MODEL_NAME": "tip-model",
        }
    )
    try:
        key, base, model = image_tools._get_vision_api_credentials()
        assert key == "tip-vision"
        assert base == "https://tip.example"
        assert model == "tip-model"
    finally:
        reset_task_env_overlay(token)


def test_get_default_models_env_fallback_uses_tip():
    from jiuwenswarm.common import config as config_module

    token = bind_task_env_overlay(
        {
            "API_KEY": "tip-key",
            "API_BASE": "https://tip-base",
            "MODEL_NAME": "tip-model",
            "MODEL_PROVIDER": "OpenAI",
        }
    )
    try:
        models = config_module.get_default_models({"models": {}})
        mcc = models[0]["model_client_config"]
        assert mcc["api_key"] == "tip-key"
        assert mcc["api_base"] == "https://tip-base"
        assert mcc["model_name"] == "tip-model"
        assert mcc["client_provider"] == "OpenAI"
    finally:
        reset_task_env_overlay(token)


def test_ssl_verify_reads_tip():
    from jiuwenswarm.agents.harness.common.tools.ssl_config import get_ssl_verify

    token = bind_task_env_overlay({"JIUWENSWARM_SSL_VERIFY": "false"})
    try:
        assert get_ssl_verify() is False
    finally:
        reset_task_env_overlay(token)
