# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host stream timeout defaults shared by single-agent / team / routing assembly."""

from __future__ import annotations

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig

from jiuwenswarm.agents.harness.common.rails.model_routing.model_routing_rail import (
    _default_model_builder,
)
from jiuwenswarm.agents.harness.team.config_loader import _build_default_model_dict
from jiuwenswarm.common.stream_timeout_defaults import (
    DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    apply_stream_timeout_defaults,
)


def test_apply_stream_timeout_defaults_fills_missing_without_mutating_source():
    source = {
        "model_name": "glm-5.3",
        "api_base": "https://example.test/v1",
        "api_key": "sk-test",
        "client_provider": "OpenAI",
    }
    out = apply_stream_timeout_defaults(source)
    assert out["stream_idle_timeout"] == DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    assert out["stream_first_chunk_timeout"] == DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
    assert "stream_idle_timeout" not in source
    assert "stream_first_chunk_timeout" not in source
    assert out is not source


def test_apply_stream_timeout_defaults_preserves_explicit_values():
    source = {
        "stream_idle_timeout": 30.0,
        "stream_first_chunk_timeout": 90.0,
        "model_name": "x",
    }
    out = apply_stream_timeout_defaults(source)
    assert out["stream_idle_timeout"] == 30.0
    assert out["stream_first_chunk_timeout"] == 90.0


def test_apply_stream_timeout_defaults_replaces_null_values():
    source = {
        "model_name": "glm-5.3",
        "stream_idle_timeout": None,
        "stream_first_chunk_timeout": None,
    }
    out = apply_stream_timeout_defaults(source)
    assert out["stream_idle_timeout"] == DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    assert out["stream_first_chunk_timeout"] == DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
    assert source["stream_idle_timeout"] is None
    assert source["stream_first_chunk_timeout"] is None


def test_team_inline_model_dict_without_ref_applies_stream_timeout_defaults():
    """Inline model MCC (no ``ref``) must still get host stream defaults."""
    from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict

    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://default.example.test/v1",
                        "api_key": "sk-default",
                        "model_name": "default-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {},
                }
            ]
        },
        "modes": {
            "team": {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {
                                    "api_base": "https://inline.example.test/v1",
                                    "api_key": "sk-inline",
                                    "model_name": "inline-model",
                                    "client_provider": "OpenAI",
                                },
                                "model_request_config": {"model": "inline-model"},
                            }
                        },
                        "teammate": {},
                    },
                }
            }
        },
    }
    spec = load_team_spec_dict(config_base=config)
    leader_mcc = spec["agents"]["leader"]["model"]["model_client_config"]
    assert leader_mcc["model_name"] == "inline-model"
    assert leader_mcc["stream_idle_timeout"] == DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    assert leader_mcc["stream_first_chunk_timeout"] == DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
    # Source template must stay untouched.
    src = config["modes"]["team"]["demo_team"]["agents"]["leader"]["model"][
        "model_client_config"
    ]
    assert "stream_idle_timeout" not in src
    assert "stream_first_chunk_timeout" not in src


def test_team_default_model_dict_applies_stream_timeout_defaults():
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://example.test/v1",
                        "api_key": "sk-test",
                        "model_name": "team-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {},
                }
            ]
        }
    }
    model = _build_default_model_dict(config)
    mcc = model["model_client_config"]
    assert mcc["stream_idle_timeout"] == DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    assert mcc["stream_first_chunk_timeout"] == DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
    assert "stream_idle_timeout" not in config["models"]["defaults"][0]["model_client_config"]
    assert "stream_first_chunk_timeout" not in config["models"]["defaults"][0]["model_client_config"]


def test_routing_default_model_builder_applies_stream_timeout_defaults():
    model = _default_model_builder(
        {
            "api_base": "https://example.test/v1",
            "api_key": "sk-test",
            "client_provider": "OpenAI",
            "verify_ssl": False,
        },
        {"model": "routed-model"},
    )
    assert isinstance(model.model_client_config, ModelClientConfig)
    assert (
        model.model_client_config.stream_idle_timeout
        == DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    )
    assert (
        model.model_client_config.stream_first_chunk_timeout
        == DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
    )
    assert isinstance(model.model_config, ModelRequestConfig)


def test_team_model_pool_metadata_includes_stream_timeout_defaults():
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    config = {
        "preferred_language": "zh",
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://a.example.test/v1",
                        "api_key": "sk-a",
                        "model_name": "model-a",
                        "client_provider": "OpenAI",
                        "verify_ssl": False,
                    },
                    "model_config_obj": {},
                },
                {
                    "model_client_config": {
                        "api_base": "https://b.example.test/v1",
                        "api_key": "sk-b",
                        "model_name": "model-b",
                        "client_provider": "OpenAI",
                        "verify_ssl": False,
                    },
                    "model_config_obj": {},
                },
            ]
        },
        "modes": {
            "team": {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {"leader": {}, "teammate": {}},
                }
            }
        },
    }
    spec = TeamManager._load_team_spec(
        "session_test_idle",
        config_base=config,
    )
    pool = getattr(spec, "model_pool", None) or []
    assert len(pool) >= 2
    for entry in pool:
        metadata = entry.get("metadata") if isinstance(entry, dict) else getattr(entry, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        client = metadata.get("client") or {}
        assert client.get("stream_idle_timeout") == DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        assert (
            client.get("stream_first_chunk_timeout")
            == DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
        )
    assert "stream_idle_timeout" not in config["models"]["defaults"][0]["model_client_config"]
    assert "stream_first_chunk_timeout" not in config["models"]["defaults"][0]["model_client_config"]
