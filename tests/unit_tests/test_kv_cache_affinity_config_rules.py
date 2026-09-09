# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.kv_cache_affinity_config import (
    build_kv_cache_affinity_config,
    is_affinity_enabled,
    model_provider,
    normalize_affinity_request,
    validate_affinity_invariant,
)


def _config(
    provider: str,
    *,
    affinity: bool = True,
    mode: str | None = None,
) -> dict:
    model_client_config: dict[str, object] = {"client_provider": provider}
    if mode is not None:
        model_client_config["extensions"] = {"kv_cache": {"mode": mode}}
    return {
        "models": {
            "defaults": [
                {
                    "is_default": True,
                    "model_client_config": model_client_config,
                }
            ]
        },
        "kv_cache_affinity_config": {
            "enable_kv_cache_affinity": affinity,
        },
        "channels": {},
    }


def test_runtime_config_fails_closed_without_capability() -> None:
    config = _config("OpenAI")

    config_module._normalize_config(config)

    assert (
        config["kv_cache_affinity_config"]["enable_kv_cache_affinity"]
        is False
    )


def test_affinity_invariant_reports_all_failures() -> None:
    valid, failures = validate_affinity_invariant(
        _config("OpenAI")
    )

    assert valid is False
    assert failures == [
        "default model must declare extensions.kv_cache.mode=affinity "
        "or use legacy provider AscendAffinity; "
        "got provider=OpenAI mode=<empty>"
    ]


def test_openai_affinity_capability_survives_runtime_normalization() -> None:
    config = _config("OpenAI", mode="affinity")

    config_module._normalize_config(config)

    assert (
        config["kv_cache_affinity_config"]["enable_kv_cache_affinity"]
        is True
    )


def test_affinity_invariant_accepts_openai_affinity_capability() -> None:
    valid, failures = validate_affinity_invariant(
        _config("OpenAI", mode="affinity")
    )

    assert valid is True
    assert failures == []


def test_affinity_invariant_accepts_legacy_ascend_alias() -> None:
    valid, failures = validate_affinity_invariant(_config("AscendAffinity"))

    assert valid is True
    assert failures == []


def test_affinity_invariant_rejects_openai_mode_none() -> None:
    valid, failures = validate_affinity_invariant(
        _config("OpenAI", mode="none")
    )

    assert valid is False
    assert failures == [
        "default model must declare extensions.kv_cache.mode=affinity "
        "or use legacy provider AscendAffinity; "
        "got provider=OpenAI mode=none"
    ]


def test_affinity_request_selects_ascend_provider() -> None:
    params = {"kv_cache_affinity_enabled": "true"}

    normalize_affinity_request(params)

    assert params["model_provider"] == "AscendAffinity"


def test_explicit_other_provider_disables_affinity() -> None:
    params = {
        "kv_cache_affinity_enabled": "true",
        "model_provider": "OpenAI",
    }

    normalize_affinity_request(params)

    assert params["kv_cache_affinity_enabled"] == "false"


def test_runtime_policy_preserves_ascend_affinity() -> None:
    model = SimpleNamespace(
        model_client_config=SimpleNamespace(client_provider="AscendAffinity")
    )

    result = build_kv_cache_affinity_config(
        _config("AscendAffinity"),
        provider=model_provider(model),
    )

    assert result.enable_kv_cache_affinity is True


def test_model_provider_prefers_original_provider_after_transport_normalization() -> None:
    model = SimpleNamespace(
        model_client_config=SimpleNamespace(
            client_provider="OpenAI",
            legacy_client_provider="DeepSeek",
        )
    )

    assert model_provider(model) == "DeepSeek"


def test_default_model_provider_prefers_original_provider_after_normalization() -> None:
    config = _config("OpenAI")
    config["models"]["defaults"][0]["model_client_config"][
        "legacy_client_provider"
    ] = "DeepSeek"

    assert config_module.get_default_model_provider(config) == "DeepSeek"


def test_runtime_policy_fails_closed_without_capability() -> None:
    result = build_kv_cache_affinity_config(
        _config("OpenAI"),
        provider="OpenAI",
    )

    assert result.enable_kv_cache_affinity is False


def test_runtime_policy_accepts_openai_affinity_capability() -> None:
    model_client_config = SimpleNamespace(
        client_provider="OpenAI",
        extensions=SimpleNamespace(
            kv_cache=SimpleNamespace(mode="affinity"),
        ),
    )

    result = build_kv_cache_affinity_config(
        _config("OpenAI", mode="affinity"),
        provider="OpenAI",
        model_client_config=model_client_config,
    )

    assert result.enable_kv_cache_affinity is True


def test_runtime_policy_rejects_openai_without_affinity_capability() -> None:
    result = build_kv_cache_affinity_config(
        _config("OpenAI", mode="none"),
        provider="OpenAI",
        model_client_config={
            "client_provider": "OpenAI",
            "extensions": {"kv_cache": {"mode": "none"}},
        },
    )

    assert result.enable_kv_cache_affinity is False


def test_legacy_react_switch_remains_readable() -> None:
    config = {
        "react": {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": True,
            }
        }
    }

    assert is_affinity_enabled(config) is True


def test_application_switch_wins_over_legacy_react_switch() -> None:
    config = {
        "kv_cache_affinity_config": {"enable_kv_cache_affinity": False},
        "react": {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": True,
            }
        },
    }

    assert is_affinity_enabled(config) is False
