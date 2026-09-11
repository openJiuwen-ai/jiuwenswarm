# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cron job model validation, incl. Opencode Zen free-model fallback.

The frontend appends Zen free models (in-memory only, never written to
config.yaml) to ``models.list`` and lets the user pick one for a cron job.
``validate_cron_model`` must resolve such an id/alias so job creation does not
fail with ``Unknown model``, while still rejecting genuinely unknown models.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.models import validate_cron_model


@pytest.fixture(autouse=True)
def _no_zen_free_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default no Zen free models are cached (clean baseline)."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [],
    )


def _user_model_entry() -> dict:
    return {
        "model_client_config": {
            "api_base": "https://api.example.com/v1",
            "api_key": "sk-test",
            "model_name": "my-model",
            "client_provider": "OpenAI",
        },
        "is_default": True,
        "alias": "我的模型",
    }


def _environment_model_entry(model_name: str = "${MODEL_NAME}") -> dict:
    return {
        "model_client_config": {
            "api_base": "${API_BASE}",
            "api_key": "${API_KEY}",
            "model_name": model_name,
            "client_provider": "${MODEL_PROVIDER}",
        },
        "is_default": True,
        "alias": "默认模型",
    }


def _zen_free_entry(
    model_id: str = "deepseek-v4-flash-free", alias: str = "DeepSeek V4 Flash"
) -> dict:
    return {
        "model_client_config": {
            "api_base": "https://opencode.ai/zen/v1",
            "api_key": "public",
            "model_name": model_id,
            "client_provider": "OpenAI",
        },
        "alias": alias,
        "is_free": True,
    }


def test_validate_cron_model_none_or_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_cron_model(None) is None
    assert validate_cron_model("") is None
    assert validate_cron_model("   ") is None


def test_validate_cron_model_resolves_user_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_model_config(name: str, index: int | None = None) -> dict | None:
        # 模拟 config.py 的真实语义：model_name 或 alias 命中都返回条目
        if name in ("my-model", "我的模型"):
            return _user_model_entry()
        return None

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        fake_get_model_config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_names", lambda: ["我的模型"]
    )
    assert validate_cron_model("my-model") == "my-model"
    # alias 也解析为 canonical model_name
    assert validate_cron_model("我的模型") == "my-model"


def test_validate_cron_model_resolves_environment_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_NAME", "deployed-model")
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_raw",
        lambda: {"models": {"defaults": [_environment_model_entry()]}},
    )

    assert validate_cron_model("deployed-model") == "deployed-model"
    assert validate_cron_model("默认模型") == "deployed-model"


def test_validate_cron_model_uses_environment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_raw",
        lambda: {
            "models": {
                "defaults": [_environment_model_entry("${MODEL_NAME:-fallback-model}")]
            }
        },
    )

    assert validate_cron_model("fallback-model") == "fallback-model"
    assert validate_cron_model("默认模型") == "fallback-model"


def test_validate_cron_model_rejects_empty_environment_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_raw",
        lambda: {"models": {"defaults": [_environment_model_entry()]}},
    )

    with pytest.raises(
        ValueError,
        match=r"Configured model '默认模型'.*resolves to an empty value.*MODEL_NAME",
    ):
        validate_cron_model("默认模型")


def test_validate_cron_model_rejects_whitespace_only_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_raw",
        lambda: {"models": {"defaults": [_environment_model_entry("   ")]}},
    )

    with pytest.raises(
        ValueError,
        match=r"Configured model '默认模型'.*resolves to an empty value",
    ):
        validate_cron_model("默认模型")


def test_validate_cron_model_accepts_zen_free_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: [])
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [_zen_free_entry()],
    )
    assert validate_cron_model("deepseek-v4-flash-free") == "deepseek-v4-flash-free"


def test_validate_cron_model_accepts_zen_free_model_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: [])
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [_zen_free_entry()],
    )
    assert validate_cron_model("DeepSeek V4 Flash") == "deepseek-v4-flash-free"


def test_validate_cron_model_unknown_model_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_names",
        lambda: ["my-model"],
    )
    # 免费模型缓存存在，但请求的模型不在其中 → 仍拒绝
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [_zen_free_entry()],
    )
    with pytest.raises(ValueError, match="Unknown model 'no-such-model'"):
        validate_cron_model("no-such-model")


def test_validate_cron_model_zen_cache_empty_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """免费模型缓存为空（Zen 不可达/开关关闭）时，免费模型 id 仍被拒绝。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: [])
    with pytest.raises(ValueError, match="Unknown model 'deepseek-v4-flash-free'"):
        validate_cron_model("deepseek-v4-flash-free")
