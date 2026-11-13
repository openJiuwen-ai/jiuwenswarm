# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.kv_cache_affinity_config import (
    build_kv_cache_affinity_config,
    NO_AFFINITY_MODEL_ERROR,
    get_default_model_client_config,
    get_kv_cache_control_model_client_config,
    get_kv_cache_control_model_client_configs,
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
    model_client_config: dict[str, object] = {"client_provider": provider, "model_name": "test-model"}
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

    assert config["kv_cache_affinity_config"]["enable_kv_cache_affinity"] is False


def test_affinity_invariant_reports_all_failures() -> None:
    valid, failures = validate_affinity_invariant(_config("OpenAI"))

    assert valid is False
    assert failures == [NO_AFFINITY_MODEL_ERROR]


def test_openai_affinity_capability_survives_runtime_normalization() -> None:
    config = _config("OpenAI", mode="affinity")

    config_module._normalize_config(config)

    assert config["kv_cache_affinity_config"]["enable_kv_cache_affinity"] is True


def test_affinity_invariant_accepts_openai_affinity_capability() -> None:
    valid, failures = validate_affinity_invariant(_config("OpenAI", mode="affinity"))

    assert valid is True
    assert failures == []


def test_affinity_invariant_accepts_legacy_ascend_alias() -> None:
    valid, failures = validate_affinity_invariant(_config("AscendAffinity"))

    assert valid is True
    assert failures == []


def test_agentos_only_affinity_configuration_is_valid() -> None:
    config = {
        "models": {
            "agentos": [
                {
                    "model_client_config": {
                        "client_provider": "AscendAffinity",
                        "model_name": "agentos-model",
                    }
                }
            ]
        },
        "kv_cache_affinity_config": {"enable_kv_cache_affinity": True},
    }

    valid, failures = validate_affinity_invariant(config)

    assert valid is True
    assert failures == []


def test_cloud_agentos_model_does_not_veto_affinity_default() -> None:
    config = _config("AscendAffinity")
    config["models"]["agentos"] = [
        {
            "model_client_config": {
                "client_provider": "OpenAI",
                "model_name": "agentos-model",
            }
        }
    ]

    valid, failures = validate_affinity_invariant(config)

    assert valid is True
    assert failures == []


def test_kvc_control_falls_back_to_default_without_agentos() -> None:
    config = _config("AscendAffinity")

    selected = get_kv_cache_control_model_client_config(config)

    assert selected is config["models"]["defaults"][0]["model_client_config"]


def test_kvc_clients_include_defaults_and_agentos() -> None:
    config = _config("OpenAI", mode="affinity")
    default_client = config["models"]["defaults"][0]["model_client_config"]
    agentos_client = {
        "client_provider": "AscendAffinity",
        "model_name": "agentos-model",
    }
    config["models"]["agentos"] = [{"model_client_config": agentos_client}]

    assert get_kv_cache_control_model_client_configs(config) == [default_client, agentos_client]
    assert get_default_model_client_config(config) is default_client


def test_affinity_invariant_rejects_openai_mode_none() -> None:
    valid, failures = validate_affinity_invariant(_config("OpenAI", mode="none"))

    assert valid is False
    assert failures == [NO_AFFINITY_MODEL_ERROR]


def test_affinity_request_does_not_change_provider() -> None:
    params = {"kv_cache_affinity_enabled": "true"}

    normalize_affinity_request(params)

    assert "model_provider" not in params


def test_openai_provider_does_not_disable_declared_affinity() -> None:
    params = {
        "kv_cache_affinity_enabled": "true",
        "model_provider": "OpenAI",
    }

    normalize_affinity_request(params)

    assert params["kv_cache_affinity_enabled"] == "true"


def test_runtime_policy_preserves_ascend_affinity() -> None:
    model = SimpleNamespace(
        model_client_config=SimpleNamespace(client_provider="AscendAffinity")
    )

    result = build_kv_cache_affinity_config(
        _config("AscendAffinity"),
        provider=model_provider(model),
    )

    assert result.enable_kv_cache_affinity is True


def test_model_provider_prefers_original_provider_after_transport_normalization() -> (
    None
):
    model = SimpleNamespace(
        model_client_config=SimpleNamespace(
            client_provider="OpenAI",
            legacy_client_provider="DeepSeek",
        )
    )

    assert model_provider(model) == "DeepSeek"


def test_default_model_provider_prefers_original_provider_after_normalization() -> None:
    config = _config("OpenAI")
    config["models"]["defaults"][0]["model_client_config"]["legacy_client_provider"] = (
        "DeepSeek"
    )

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


def test_empty_affinity_template_does_not_admit_switch() -> None:
    config = _config("OpenAI", mode="affinity")
    config["models"]["defaults"][0]["model_client_config"]["model_name"] = "  "
    assert validate_affinity_invariant(config) == (False, [NO_AFFINITY_MODEL_ERROR])


def test_backup_affinity_survives_load_with_cloud_default_and_agentos() -> None:
    config = _config("OpenAI")
    backup = _config("OpenAI", mode="affinity")["models"]["defaults"][0]
    backup["is_default"] = False
    config["models"]["defaults"].append(backup)
    config["models"]["agentos"] = _config("OpenAI")["models"]["defaults"]
    config_module._normalize_config(config)
    assert is_affinity_enabled(config) is True
