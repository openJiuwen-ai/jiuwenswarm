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


def _zen_free_entry(model_id: str = "deepseek-v4-flash-free", alias: str = "DeepSeek V4 Flash") -> dict:
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


def test_validate_cron_model_resolves_user_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_model_config(name: str, index: int | None = None) -> dict | None:
        # 模拟 config.py 的真实语义：model_name 或 alias 命中都返回条目
        if name in ("my-model", "我的模型"):
            return _user_model_entry()
        return None

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        fake_get_model_config,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: ["我的模型"])
    assert validate_cron_model("my-model") == "my-model"
    # alias 也解析为 canonical model_name
    assert validate_cron_model("我的模型") == "my-model"


def _shipped_env_entry() -> dict:
    """Mirror the shipped config shape: model_name via a ${MODEL_NAME} placeholder."""
    return {
        "model_client_config": {
            "api_base": "https://api.example.com/v1",
            "api_key": "sk-test",
            "model_name": "${MODEL_NAME}",
            "client_provider": "OpenAI",
        },
        "is_default": True,
        "alias": "default-model",
    }


def _patch_shipped_env_config(monkeypatch: pytest.MonkeyPatch, entry: dict) -> None:
    """Fake get_model_config with the real matching semantics (resolve-then-compare)."""
    from jiuwenswarm.common.config import resolve_env_vars

    def fake_get_model_config(name: str, index: int | None = None) -> dict | None:
        mcc = entry.get("model_client_config") or {}
        if resolve_env_vars(str(mcc.get("model_name") or "")) == name:
            return entry
        alias = entry.get("alias") or ""
        if alias and resolve_env_vars(str(alias)) == name:
            return entry
        return None

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        fake_get_model_config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_names", lambda: [entry.get("alias", "")]
    )


def test_validate_cron_model_resolves_env_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """${MODEL_NAME} entries must store the resolved cache key, not the placeholder."""
    monkeypatch.setenv("MODEL_NAME", "deployed-model")
    _patch_shipped_env_config(monkeypatch, _shipped_env_entry())
    # Both the alias and the resolved name map to the live AgentServer cache key.
    assert validate_cron_model("default-model") == "deployed-model"
    assert validate_cron_model("deployed-model") == "deployed-model"


def test_validate_cron_model_honors_env_default_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """${VAR:-default} falls back to the default when the variable is unset."""
    monkeypatch.delenv("MODEL_NAME", raising=False)
    entry = _shipped_env_entry()
    entry["model_client_config"]["model_name"] = "${MODEL_NAME:-fallback-model}"
    _patch_shipped_env_config(monkeypatch, entry)
    assert validate_cron_model("default-model") == "fallback-model"


def test_validate_cron_model_rejects_unset_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare placeholder resolving to empty must fail fast before persistence."""
    monkeypatch.delenv("MODEL_NAME", raising=False)
    _patch_shipped_env_config(monkeypatch, _shipped_env_entry())
    with pytest.raises(ValueError, match="resolves to an empty value"):
        validate_cron_model("default-model")


def test_validate_cron_model_rejects_whitespace_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only configured name is not a valid canonical cache key."""
    entry = _shipped_env_entry()
    entry["model_client_config"]["model_name"] = "   "
    _patch_shipped_env_config(monkeypatch, entry)
    with pytest.raises(ValueError, match="resolves to an empty value"):
        validate_cron_model("default-model")


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
