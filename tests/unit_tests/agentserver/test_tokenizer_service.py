# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime import tokenizer_service as tokenizer_service_module
from jiuwenswarm.server.runtime.tokenizer_service import TokenizerService


def _config(
    cache_dir,
    *,
    enabled: bool = True,
    models: list[dict] | None = None,
) -> dict:
    context_config = {
        "enable_tiktoken_counter": enabled,
        "tokenizer_cache_dir": str(cache_dir),
    }
    return {
        "react": {
            "context_engine_config": context_config,
        },
        "models": {"defaults": models or []},
    }


@pytest.mark.asyncio
async def test_tokenizer_service_skips_warmup_when_counter_switch_is_false(tmp_path):
    service = TokenizerService()

    result = await service.warm(
        _config(
            tmp_path / "cache",
            enabled=False,
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "gpt-4o",
                    }
                }
            ],
        ),
        reason="test",
    )

    assert result["enabled"] is False
    assert result["counter_enabled"] is False
    assert result["warmed"] == 0
    assert result["skipped"] == 1
    assert result["models"] == []
    assert not (tmp_path / "cache").exists()


@pytest.mark.asyncio
async def test_disabled_warmup_does_not_query_repository_metadata(
    tmp_path, monkeypatch
):
    import huggingface_hub

    def fail_if_metadata_is_requested(*args, **kwargs):
        raise AssertionError("metadata discovery must stay disabled")

    monkeypatch.setattr(huggingface_hub, "HfApi", fail_if_metadata_is_requested)
    result = await TokenizerService().warm(
        _config(
            tmp_path / "cache",
            enabled=False,
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "acme/adapter",
                    }
                }
            ],
        ),
        reason="disabled",
    )

    assert result["enabled"] is False
    assert result["skipped"] == 1
    assert not (tmp_path / "cache").exists()


@pytest.mark.asyncio
async def test_tokenizer_service_warms_new_model_only_once(tmp_path, monkeypatch):
    calls: list[dict] = []

    async def fake_warm_one(self, profile, settings):
        calls.append({"model": profile.model, "settings": settings})
        return SimpleNamespace(
            measurement_source="native_tokenizer",
            measurement_tokenizer="gpt-4o",
            measurement_fallback_reason=None,
        )

    # _warm_one owns the core interaction; stub it to test dedup semantics.
    monkeypatch.setattr(TokenizerService, "_warm_one", fake_warm_one)
    service = TokenizerService()
    first_models = [
        {
            "model_client_config": {
                "client_provider": "OpenAI",
                "model_name": "gpt-4o",
            }
        }
    ]
    second_models = first_models + [
        {
            "model_client_config": {
                "client_provider": "DeepSeek",
                "model_name": "deepseek-chat",
            }
        }
    ]

    first = await service.warm(
        _config(tmp_path / "cache", models=first_models), reason="startup"
    )
    second = await service.warm(
        _config(tmp_path / "cache", models=second_models), reason="model config change"
    )

    assert first["warmed"] == 1
    assert second["warmed"] == 1
    assert second["skipped"] == 1
    assert [call["model"] for call in calls] == ["gpt-4o", "deepseek-chat"]
    assert all(call["settings"].cache_dir == tmp_path / "cache" for call in calls)
    assert all(call["settings"].offline is False for call in calls)


@pytest.mark.asyncio
async def test_tokenizer_service_fallback_is_degraded_and_retryable(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    async def fake_warm_one(self, profile, settings):
        calls.append(profile.model)
        return SimpleNamespace(
            measurement_source="string_length_fallback",
            measurement_tokenizer="unicode_codepoints",
            measurement_fallback_reason="native_tokenizer_unavailable",
            measurement_fallback_tokenizer_model="deepseek-v4-flash",
        )

    monkeypatch.setattr(TokenizerService, "_warm_one", fake_warm_one)
    service = TokenizerService()
    config = _config(
        tmp_path / "cache",
        models=[
            {
                "model_client_config": {
                    "client_provider": "DeepSeek",
                    "model_name": "deepseek-chat",
                }
            }
        ],
    )

    first = await service.warm(config, reason="startup")
    second = await service.warm(config, reason="retry")

    assert first["warmed"] == 0
    assert first["degraded"] == 1
    assert first["failed"] == 0
    assert first["models"][0]["status"] == "fallback"
    assert first["models"][0]["fallback_tokenizer_model"] == "deepseek-v4-flash"
    assert second["warmed"] == 0
    assert second["degraded"] == 1
    assert calls == ["deepseek-chat", "deepseek-chat"]


@pytest.mark.asyncio
async def test_tokenizer_service_retries_remote_warmup_after_transient_failure(
    tmp_path, monkeypatch
):
    import openjiuwen.core.context_engine as context_engine

    attempts = 0

    class FakeManager:
        def __init__(self, **kwargs):
            self.last_error = None

    class FakeSelector:
        def __init__(self, *, manager, **kwargs):
            self.manager = manager

        def select(self):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                self.manager.last_error = "tokenizer_download_failed"
                return SimpleNamespace(
                    measurement_source="string_length_fallback",
                    measurement_tokenizer="unicode_codepoints",
                    measurement_fallback_reason="native_tokenizer_unavailable",
                )
            return SimpleNamespace(
                measurement_source="native_tokenizer",
                measurement_tokenizer="zai-org/GLM-5.2",
                measurement_fallback_reason=None,
            )

    monkeypatch.setattr(context_engine, "TokenizerArtifactManager", FakeManager)
    monkeypatch.setattr(context_engine, "TokenizerSelector", FakeSelector)
    monkeypatch.setattr(
        tokenizer_service_module,
        "_TOKENIZER_WARMUP_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )

    result = await TokenizerService().warm(
        _config(
            tmp_path / "cache",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "GLM-5.2",
                    }
                }
            ],
        ),
        reason="startup",
    )

    assert attempts == 3
    assert result["warmed"] == 1
    assert result["degraded"] == 0
    assert result["models"][0]["status"] == "native_warmed"


@pytest.mark.asyncio
async def test_configured_model_metadata_is_case_insensitive_and_deduplicated(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    async def fake_warm_one(self, profile, settings):
        calls.append(f"{profile.provider}:{profile.model}")
        return SimpleNamespace(
            measurement_source="native_tokenizer",
            measurement_tokenizer="glm-5.2",
            measurement_fallback_reason=None,
        )

    monkeypatch.setattr(TokenizerService, "_warm_one", fake_warm_one)
    service = TokenizerService()
    config = _config(
        tmp_path / "cache",
        enabled=True,
        models=[
            {
                "model_client_config": {
                    "client_provider": "OpenAI",
                    "model_name": "GLM-5.2",
                }
            },
            {
                "model_client_config": {
                    "client_provider": "openai",
                    "model_name": "glm-5.2",
                }
            },
        ],
    )

    result = await service.warm(config, reason="startup")

    assert result["enabled"] is True
    assert result["counter_enabled"] is True
    assert result["warmed"] == 1
    assert calls == ["OpenAI:GLM-5.2"]


def test_model_tokenizer_metadata_can_be_read_without_nested_spec(tmp_path):
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            tmp_path / "cache",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "glm-5.2-thinking",
                        "tokenizer_id": "ZAI/GLM-5.2",
                        "tokenizer_source": "HUGGINGFACE",
                        "tokenizer_engine": "TOKENIZERS",
                    }
                }
            ],
        )
    )

    assert profiles[0].spec == {
        "id": "ZAI/GLM-5.2",
        "source": "HUGGINGFACE",
        "engine": "TOKENIZERS",
        "provider": "OpenAI",
        "model": "glm-5.2-thinking",
    }


def test_configured_known_models_get_default_tokenizer_specs():
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            "/tmp/tokenizers",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "GLM-5.2-thinking",
                    }
                },
                {
                    "model_client_config": {
                        "client_provider": "DeepSeek",
                        "model_name": "deepseek-v4-flash",
                    }
                },
            ],
        )
    )

    assert profiles[0].spec == {
        "provider": "OpenAI",
        "model": "glm-5.2",
        "id": "zai-org/GLM-5.2",
        "source": "huggingface",
        "engine": "tokenizers",
        "compatible_fallbacks": [
            {
                "model": "glm-5",
                "id": "zai-org/GLM-5",
                "source": "huggingface",
                "engine": "tokenizers",
            }
        ],
    }
    assert profiles[1].spec == {
        "provider": "DeepSeek",
        "model": "deepseek-v4-flash",
        "id": "deepseek-ai/DeepSeek-V4-Flash",
        "source": "huggingface",
        "engine": "tokenizers",
    }


def test_deepseek_v4_pro_uses_v4_flash_as_its_only_family_fallback():
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            "/tmp/tokenizers",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "DeepSeek",
                        "model_name": "DeepSeek-V4-pro_lora",
                    }
                }
            ],
        )
    )

    assert profiles[0].spec == {
        "provider": "DeepSeek",
        "model": "deepseek-v4-pro",
        "id": "deepseek-ai/DeepSeek-V4-Pro",
        "source": "huggingface",
        "engine": "tokenizers",
        "compatible_fallbacks": [
            {
                "model": "deepseek-v4-flash",
                "id": "deepseek-ai/DeepSeek-V4-Flash",
                "source": "huggingface",
                "engine": "tokenizers",
            }
        ],
    }


def test_deepseek_v4_flash_is_a_canonical_fallback_without_chain():
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            "/tmp/tokenizers",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "DeepSeek",
                        "model_name": "deepseek-v4-flash_extra",
                    }
                }
            ],
        )
    )

    assert profiles[0].spec == {
        "provider": "DeepSeek",
        "model": "deepseek-v4-flash",
        "id": "deepseek-ai/DeepSeek-V4-Flash",
        "source": "huggingface",
        "engine": "tokenizers",
    }


@pytest.mark.parametrize(
    ("model_name", "expected_model", "expected_tokenizer", "expected_engine"),
    [
        ("QWEN3.8-MAX", "qwen3.8", "Qwen/Qwen3.8-27B", "tokenizers"),
        ("qwen3.8_lora", "qwen3.8", "Qwen/Qwen3.8-27B", "tokenizers"),
        ("KIMI-K2.5", "kimi-k2.5", "moonshotai/Kimi-K2.5", "tiktoken"),
        ("KIMI-K2.6", "kimi-k2.6", "moonshotai/Kimi-K2.6", "tiktoken"),
        (
            "moonshotai/kimi-k2.6",
            "moonshotai/kimi-k2.6",
            "moonshotai/Kimi-K2.6",
            "tiktoken",
        ),
        ("KIMI-K2.7-CODE", "kimi-k2.7-code", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ],
)
def test_qwen_and_kimi_variants_get_family_tokenizer_specs(
    model_name,
    expected_model,
    expected_tokenizer,
    expected_engine,
):
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            "/tmp/tokenizers",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": model_name,
                    }
                }
            ],
        )
    )

    expected = {
        "provider": "OpenAI",
        "model": expected_model,
        "id": expected_tokenizer,
        "source": "huggingface",
        "engine": expected_engine,
    }
    if expected_model == "qwen3.8":
        expected["compatible_fallbacks"] = [
            {
                "model": "qwen3-8b",
                "id": "Qwen/Qwen3-8B",
                "source": "huggingface",
                "engine": "tokenizers",
            }
        ]
    elif expected_model != "kimi-k2.7-code":
        expected["compatible_fallbacks"] = [
            {
                "model": "kimi-k2.7",
                "id": "moonshotai/Kimi-K2.7-Code",
                "source": "huggingface",
                "engine": "tiktoken",
            }
        ]
    assert profiles[0].spec == expected


def test_kimi_k3_gets_k2_7_compatible_fallback():
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            "/tmp/tokenizers",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "KIMI-K3",
                    }
                }
            ],
        )
    )

    assert profiles[0].spec == {
        "provider": "OpenAI",
        "model": "kimi-k3",
        "id": "moonshotai/Kimi-K3",
        "source": "huggingface",
        "engine": "tiktoken",
        "compatible_fallbacks": [
            {
                "model": "kimi-k2.7",
                "id": "moonshotai/Kimi-K2.7-Code",
                "source": "huggingface",
                "engine": "tiktoken",
            }
        ],
    }


def test_unknown_alias_remains_unresolved_without_tokenizer_metadata():
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            "/tmp/tokenizers",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "internal-prod-alias",
                    }
                }
            ],
        )
    )

    assert profiles[0].spec is None


def test_repository_metadata_cache_is_bounded_and_expires(monkeypatch):
    import huggingface_hub

    now = [0.0]
    calls: list[str] = []

    class FakeApi:
        def __init__(self, *, endpoint):
            assert endpoint == tokenizer_service_module._HUGGINGFACE_ENDPOINT

        def model_info(self, repo_id, **kwargs):
            del kwargs
            calls.append(repo_id)
            return SimpleNamespace(
                id=repo_id,
                siblings=[],
                cardData={},
                config={},
            )

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(
        tokenizer_service_module.time,
        "monotonic",
        lambda: now[0],
    )
    monkeypatch.setattr(
        tokenizer_service_module,
        "_TOKENIZER_METADATA_CACHE_MAX_ENTRIES",
        2,
    )
    monkeypatch.setattr(
        tokenizer_service_module,
        "_TOKENIZER_METADATA_CACHE_TTL_SECONDS",
        10.0,
    )
    tokenizer_service_module._TOKENIZER_METADATA_CACHE.clear()

    for repo_id in ("acme/one", "acme/two"):
        assert tokenizer_service_module._huggingface_repository_metadata(
            repo_id,
            allow_network=True,
        ) is not None

    # A cache hit refreshes recency, so ``acme/two`` is evicted first.
    assert tokenizer_service_module._huggingface_repository_metadata(
        "acme/one",
        allow_network=True,
    ) is not None
    assert tokenizer_service_module._huggingface_repository_metadata(
        "acme/three",
        allow_network=True,
    ) is not None
    assert list(tokenizer_service_module._TOKENIZER_METADATA_CACHE) == [
        ("acme/one", None),
        ("acme/three", None),
    ]
    assert "acme/two" in calls
    calls_before_expiry = len(calls)

    now[0] = 11.0
    assert tokenizer_service_module._huggingface_repository_metadata(
        "acme/one",
        allow_network=True,
    ) is not None
    assert len(calls) == calls_before_expiry + 1


def test_repository_metadata_discovery_is_warmup_only_and_adds_one_fallback(
    monkeypatch,
):
    import huggingface_hub

    calls: list[tuple[str, float | None]] = []

    class FakeApi:
        def __init__(self, *, endpoint):
            assert endpoint == tokenizer_service_module._HUGGINGFACE_ENDPOINT

        def model_info(self, repo_id, *, revision=None, timeout=None, **kwargs):
            del revision, kwargs
            calls.append((repo_id, timeout))
            if repo_id == "acme/adapter":
                return SimpleNamespace(
                    id=repo_id,
                    siblings=[SimpleNamespace(rfilename="tiktoken.model")],
                    cardData={"base_model": "acme/base"},
                    config={},
                )
            return SimpleNamespace(
                id=repo_id,
                siblings=[SimpleNamespace(rfilename="tokenizer.json")],
                cardData={},
                config={},
            )

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    tokenizer_service_module._TOKENIZER_METADATA_CACHE.clear()

    assert (
        tokenizer_service_module._huggingface_repository_metadata(
            "acme/adapter",
            allow_network=False,
        )
        is None
    )
    assert calls == []

    profile = tokenizer_service_module.TokenizerProfile(
        provider="OpenAI",
        model="acme/adapter",
        spec=tokenizer_service_module._default_tokenizer_spec(
            provider="OpenAI",
            model="acme/adapter",
        ),
        allow_metadata_discovery=True,
    )
    spec = tokenizer_service_module._discover_tokenizer_spec(
        profile,
        allow_network=True,
    )

    assert spec is not None
    assert spec["engine"] == "tiktoken"
    assert spec["compatible_fallbacks"] == [
        {
            "model": "acme/base",
            "id": "acme/base",
            "source": "huggingface",
            "engine": "tokenizers",
        }
    ]
    assert [repo_id for repo_id, _ in calls] == ["acme/adapter", "acme/base"]

    # Once warm-up has discovered the metadata, profile construction only
    # reads the in-process cache; it does not perform another network call.
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            "/tmp/tokenizers",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "acme/adapter",
                    }
                }
            ],
        )
    )
    assert profiles[0].spec == spec
    assert len(calls) == 2


def test_repository_metadata_can_choose_a_same_family_sibling(monkeypatch):
    import huggingface_hub

    class FakeApi:
        def __init__(self, *, endpoint):
            assert endpoint == tokenizer_service_module._HUGGINGFACE_ENDPOINT

        def model_info(self, repo_id, **kwargs):
            del kwargs
            return SimpleNamespace(
                id=repo_id,
                siblings=[SimpleNamespace(rfilename="tokenizer.json")],
                cardData={},
                config={"model_type": "acme_transformer"},
                tags=["acme_transformer"],
            )

        def list_models(self, **kwargs):
            assert kwargs["author"] == "acme"
            assert kwargs["filter"] == "acme_transformer"
            return [
                SimpleNamespace(
                    id="acme/base-model",
                    siblings=[SimpleNamespace(rfilename="tokenizer.json")],
                    config={"model_type": "acme_transformer"},
                    tags=["acme_transformer"],
                )
            ]

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    tokenizer_service_module._TOKENIZER_METADATA_CACHE.clear()
    profile = tokenizer_service_module.TokenizerProfile(
        provider="OpenAI",
        model="acme/adapter-v2",
        spec=tokenizer_service_module._default_tokenizer_spec(
            provider="OpenAI",
            model="acme/adapter-v2",
        ),
        allow_metadata_discovery=True,
    )

    spec = tokenizer_service_module._discover_tokenizer_spec(
        profile,
        allow_network=True,
    )

    assert spec is not None
    assert spec["compatible_fallbacks"][0]["id"] == "acme/base-model"


def test_explicit_repository_spec_is_not_overwritten_by_metadata_discovery(
    tmp_path,
):
    profiles = tokenizer_service_module.configured_tokenizer_profiles(
        _config(
            tmp_path / "cache",
            models=[
                {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "model_name": "acme/adapter",
                        "tokenizer_spec": {
                            "id": "acme/exact-tokenizer",
                            "source": "huggingface",
                            "engine": "tokenizers",
                        },
                    }
                }
            ],
        )
    )

    assert profiles[0].allow_metadata_discovery is False
    assert profiles[0].spec["id"] == "acme/exact-tokenizer"
    assert "compatible_fallbacks" not in profiles[0].spec


@pytest.mark.asyncio
async def test_model_variants_share_one_registered_tokenizer_warmup(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    async def fake_warm_one(self, profile, settings):
        calls.append(profile.model)
        return SimpleNamespace(
            measurement_source="native_tokenizer",
            measurement_tokenizer="glm-5.2",
            measurement_fallback_reason=None,
        )

    monkeypatch.setattr(TokenizerService, "_warm_one", fake_warm_one)
    config = _config(
        tmp_path / "cache",
        models=[
            {
                "model_client_config": {
                    "client_provider": "OpenAI",
                    "model_name": "GLM-5.2",
                }
            },
            {
                "model_client_config": {
                    "client_provider": "openai",
                    "model_name": "glm-5.2-thinking",
                }
            },
        ],
    )
    config["react"]["context_engine_config"]["tokenizer_registry"] = [
        {
            "provider": "OPENAI",
            "model": "glm-5.2",
            "source": "LOCAL",
            "artifact_path": str(tmp_path / "tokenizer.json"),
        }
    ]

    profiles = tokenizer_service_module.configured_tokenizer_profiles(config)
    assert [profile.spec["source"] for profile in profiles] == ["local", "local"]

    result = await TokenizerService().warm(config, reason="startup")

    assert result["warmed"] == 1
    assert result["skipped"] == 1
    assert calls == ["GLM-5.2"]
