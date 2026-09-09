import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError, build_error
from openjiuwen.core.foundation.llm import (
    Model,
    ModelClientConfig,
    ModelRequestConfig,
)

from jiuwenswarm.symphony.adapter import llm_config_signature
from jiuwenswarm.symphony.llm import (
    LLMConfig,
    create_llm_client,
    create_model_response_observer,
    extract_message_content,
    get_llm_token_usage_summary,
    probe_model_connection,
    register_request_model,
    resolve_request_llm_config,
    reset_llm_token_usage,
    thinking_disabled_request_overrides,
    _record_usage_from_response,
)


class _FakeInvokeModel:
    def __init__(self):
        self.calls = []

    async def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content='{"ok": true}')


def _model_entry(*, reasoning_level=None, client=None, request=None):
    client_config = {
        "api_key": "key",
        "api_base": "https://example.test/v1",
        "model_name": "model-a",
        "client_provider": "openai",
        **(client or {}),
    }
    request_config = dict(request or {})
    if reasoning_level is not None:
        request_config["reasoning_level"] = reasoning_level
    return {
        "model_client_config": client_config,
        "model_config_obj": request_config,
    }


def _llm_config():
    return LLMConfig(
        model="model-a",
        model_client_config=_model_entry()["model_client_config"],
    )


def test_thinking_disabled_request_overrides_returns_isolated_compatibility_fields():
    first = thinking_disabled_request_overrides()
    second = thinking_disabled_request_overrides()

    assert first == {
        "extra_body": {
            "thinking": {"type": "disabled"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    }
    first["extra_body"]["thinking"]["type"] = "enabled"
    first["extra_body"]["chat_template_kwargs"]["enable_thinking"] = True

    assert second["extra_body"]["thinking"]["type"] == "disabled"
    assert second["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_extract_message_content_supports_openjiuwen_response_shape():
    response = SimpleNamespace(content=[{"text": '{"ok": true}'}])

    assert extract_message_content(response) == '{"ok": true}'


def test_llm_config_from_default_models(monkeypatch):
    model_config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {},
                    "model_config_obj": {},
                }
            ]
        }
    }
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: model_config)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_default_models",
        lambda config=None: [
            {
                "model_client_config": {
                    "api_key": "key",
                    "api_base": "https://example.test/v1/",
                    "model_name": "model-a",
                    "client_provider": "openai",
                    "custom_headers": {"X-Test": "1"},
                    "timeout": 12,
                    "verify_ssl": False,
                },
                "model_config_obj": {
                    "temperature": 0.2,
                    "top_p": 0.8,
                    "max_tokens": 99,
                },
            }
        ],
    )

    config = LLMConfig.from_default_model()

    assert config.model_client_config["api_key"] == "key"
    assert config.base_url == "https://example.test/v1"
    assert config.model == "model-a"
    assert config.model_client_config["client_provider"] == "openai"
    assert "timeout_seconds" not in LLMConfig.__dataclass_fields__
    assert "max_tokens" not in LLMConfig.__dataclass_fields__
    assert config.temperature == 0.0
    assert config.top_p == 1.0
    assert "batch_size" not in LLMConfig.__dataclass_fields__
    assert not hasattr(config, "timeout_seconds")
    assert not hasattr(config, "max_tokens")
    assert config.model_client_kwargs()["custom_headers"] == {"X-Test": "1"}
    assert config.model_client_kwargs()["timeout"] == 12
    assert config.model_client_kwargs()["verify_ssl"] is False
    assert config.model_request_kwargs()["temperature"] == 0.0
    assert config.model_request_kwargs()["top_p"] == 1.0
    assert config.model_request_kwargs()["max_tokens"] == 99


def test_llm_config_from_runtime_model_uses_selected_request_model():
    model = SimpleNamespace(
        model_client_config=SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "api_base": "https://selected.example/v1",
                "api_key": "selected-key",
                "client_provider": "OpenAI",
                "model_name": "selected-model",
            }
        ),
        model_config=SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "model": "selected-model",
                "temperature": 0.2,
            }
        ),
    )

    config = LLMConfig.from_model(model)

    assert config.model == "selected-model"
    assert config.base_url == "https://selected.example/v1"
    assert config.model_client_config["api_key"] == "selected-key"


def test_llm_config_from_real_core_model_reads_model_request_field_name():
    def real_model():
        return Model(
            model_client_config=ModelClientConfig(
                api_base="https://selected.example/v1",
                api_key="selected-key",
                client_provider="OpenAI",
            ),
            model_config=ModelRequestConfig(
                model="selected-model",
                temperature=0.2,
            ),
        )

    first = LLMConfig.from_model(real_model())
    second = LLMConfig.from_model(real_model())

    assert first.model == "selected-model"
    assert first.base_url == "https://selected.example/v1"
    assert "client_id" not in first.model_client_config
    assert first.identity_digest() == second.identity_digest()
    assert first.create_model().model_config.model_name == "selected-model"


def test_request_model_registry_reuses_stable_effective_config_identity():
    def real_model():
        return Model(
            model_client_config=ModelClientConfig(
                api_base="https://selected.example/v1",
                api_key="selected-key",
                client_provider="OpenAI",
            ),
            model_config=ModelRequestConfig(
                model="selected-model",
                temperature=0.2,
            ),
        )

    first_reference = register_request_model(real_model())
    second_reference = register_request_model(real_model())

    assert first_reference == second_reference
    assert resolve_request_llm_config(first_reference).model == "selected-model"


@pytest.mark.parametrize(
    ("client_provider", "auth_mode", "api_mode"),
    [
        ("OpenAI", "none", None),
        ("OpenAI", "custom_headers", None),
        ("OpenAI", "openai_account_oauth", None),
        ("OpenAIAccount", None, "responses"),
    ],
)
def test_llm_config_from_runtime_model_accepts_native_no_key_auth_modes(
    client_provider,
    auth_mode,
    api_mode,
):
    client_config = {
        "api_base": "https://selected.example/v1",
        "client_provider": client_provider,
        "model_name": "selected-model",
    }
    if auth_mode is not None:
        client_config["auth_mode"] = auth_mode
    if api_mode is not None:
        client_config["api_mode"] = api_mode
    model = SimpleNamespace(
        model_client_config=client_config,
        model_config={"model": "selected-model"},
    )

    config = LLMConfig.from_model(model)

    assert config.model == "selected-model"
    assert config.model_client_config.get("api_key") in (None, "")


def test_llm_config_removes_internal_reasoning_level():
    config = LLMConfig.from_model_entry(
        _model_entry(reasoning_level="off", request={"max_tokens": 99})
    )

    request_kwargs = config.model_request_kwargs()

    assert "reasoning_level" not in request_kwargs
    assert "reasoning" not in request_kwargs
    assert request_kwargs["max_tokens"] == 99
    assert (
        request_kwargs["extra_body"]
        == thinking_disabled_request_overrides()["extra_body"]
    )


def test_llm_config_forces_high_reasoning_config_to_disabled():
    config = LLMConfig.from_model_entry(
        _model_entry(
            reasoning_level="high",
            client={
                "api_base": "https://api.deepseek.com",
                "model_name": "deepseek-v4-pro",
            },
            request={
                "max_tokens": 99,
                "reasoning": {"mode": "enabled", "effort": "max"},
                "reasoning_effort": "high",
                "thinking": {"type": "enabled"},
                "enable_thinking": True,
                "chat_template_kwargs": {"enable_thinking": True},
                "extra_body": {
                    "custom_option": {"enabled": True},
                    "reasoning": {"effort": "high"},
                    "thinking": {"type": "enabled"},
                    "enable_thinking": True,
                    "thinking_budget": 4096,
                    "chat_template_kwargs": {"enable_thinking": True},
                },
            },
        )
    )

    request_kwargs = config.model_request_kwargs()

    assert "reasoning_level" not in request_kwargs
    assert "reasoning" not in request_kwargs
    assert "reasoning_effort" not in request_kwargs
    assert "thinking" not in request_kwargs
    assert "enable_thinking" not in request_kwargs
    assert "chat_template_kwargs" not in request_kwargs
    assert request_kwargs["max_tokens"] == 99
    assert request_kwargs["extra_body"] == {
        "custom_option": {"enabled": True},
        **thinking_disabled_request_overrides()["extra_body"],
    }


def test_llm_config_legacy_controls_reach_core_without_neutral_reasoning_plan(
    monkeypatch,
):
    config = LLMConfig.from_model_entry(
        _model_entry(
            reasoning_level="high",
            client={
                "api_base": "https://custom.example.test/v1",
                "model_name": "deepseek-v4-flash",
            },
        )
    )

    captured = {}

    class FakeModel:
        def __init__(self, *, model_client_config, model_config):
            captured["client"] = model_client_config
            captured["request"] = model_config

    monkeypatch.setattr("openjiuwen.core.foundation.llm.Model", FakeModel)

    model = config.create_model()

    assert isinstance(model, FakeModel)
    assert captured["request"].reasoning is None
    assert (
        captured["request"].extra_body
        == thinking_disabled_request_overrides()["extra_body"]
    )


def test_llm_config_owns_nested_model_entry_data():
    entry = _model_entry(
        reasoning_level="off",
        client={
            "custom_headers": {"X-Test": "original"},
        },
        request={
            "response_format": {"type": "json_object"},
            "extra_body": {"custom_option": {"enabled": True}},
        },
    )
    original = deepcopy(entry)

    config = LLMConfig.from_model_entry(entry)
    client_kwargs = config.model_client_kwargs()
    request_kwargs = config.model_request_kwargs()
    client_kwargs["custom_headers"]["X-Test"] = "changed"
    request_kwargs["response_format"]["type"] = "text"
    request_kwargs["extra_body"]["custom_option"]["enabled"] = False

    assert entry == original
    assert config.model_client_kwargs()["custom_headers"] == {"X-Test": "original"}
    assert config.model_request_kwargs()["response_format"] == {"type": "json_object"}
    assert config.model_request_kwargs()["extra_body"]["custom_option"] == {
        "enabled": True
    }


def test_llm_config_prefers_resolved_default_model(monkeypatch):
    model_config = {
        "models": {
            "defaults": [
                {"model_client_config": {}, "model_config_obj": {}},
                {"model_client_config": {}, "model_config_obj": {}},
            ]
        }
    }
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: model_config)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_default_models",
        lambda config=None: [
            {
                "is_default": False,
                "model_client_config": {
                    "api_key": "key-a",
                    "api_base": "https://a.example.test/v1",
                    "model_name": "model-a",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            },
            {
                "is_default": True,
                "model_client_config": {
                    "api_key": "key-b",
                    "api_base": "https://b.example.test/v1",
                    "model_name": "model-b",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            },
        ],
    )

    config = LLMConfig.from_default_model()

    assert config.model == "model-b"
    assert config.base_url == "https://b.example.test/v1"


def test_llm_config_does_not_fallback_to_environment_model(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {"models": {}})
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv("API_BASE", "https://example.test/v1")
    monkeypatch.setenv("MODEL_NAME", "model-a")

    with pytest.raises(RuntimeError, match="config.yaml"):
        LLMConfig.from_default_model()


def test_create_llm_client_uses_jiuwenswarm_client():
    client = create_llm_client(_llm_config())

    assert type(client).__name__ == "JiuwenSwarmChatClient"


def test_llm_config_creates_native_openjiuwen_model(monkeypatch):
    captured = {}

    class FakeModel:
        def __init__(self, *, model_client_config, model_config):
            captured["client"] = model_client_config
            captured["request"] = model_config

    monkeypatch.setattr("openjiuwen.core.foundation.llm.Model", FakeModel)

    model = _llm_config().create_model()

    assert isinstance(model, FakeModel)
    assert captured["request"].model_name == "model-a"
    assert captured["client"].api_base == "https://example.test/v1"


@pytest.mark.asyncio
async def test_probe_model_connection_uses_bounded_low_cost_request(monkeypatch):
    model = _FakeInvokeModel()
    monkeypatch.setattr(LLMConfig, "create_model", lambda _config: model)

    await probe_model_connection(_llm_config())

    assert model.calls == [
        {
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 16,
            "timeout": 25,
        }
    ]


@pytest.mark.asyncio
async def test_probe_model_connection_preserves_framework_model_error(monkeypatch):
    expected = build_error(
        StatusCode.MODEL_CALL_FAILED,
        error_msg="model unavailable",
    )

    class FailingModel:
        async def invoke(self, **kwargs):
            del kwargs
            raise expected

    monkeypatch.setattr(LLMConfig, "create_model", lambda _config: FailingModel())

    with pytest.raises(BaseError) as exc_info:
        await probe_model_connection(_llm_config())

    assert exc_info.value is expected
    assert (
        str(exc_info.value) == "[181001] model call failed, reason: model unavailable"
    )


@pytest.mark.asyncio
async def test_probe_model_connection_wraps_untyped_provider_error(monkeypatch):
    class FailingModel:
        async def invoke(self, **kwargs):
            del kwargs
            raise OSError("connection refused")

    monkeypatch.setattr(LLMConfig, "create_model", lambda _config: FailingModel())

    with pytest.raises(BaseError) as exc_info:
        await probe_model_connection(_llm_config())

    assert exc_info.value.status is StatusCode.MODEL_CALL_FAILED
    assert (
        str(exc_info.value) == "[181001] model call failed, reason: connection refused"
    )
    assert isinstance(exc_info.value.cause, OSError)


@pytest.mark.asyncio
async def test_probe_model_connection_enforces_coroutine_deadline(monkeypatch):
    class HangingModel:
        async def invoke(self, **kwargs):
            del kwargs
            await asyncio.Event().wait()

    monkeypatch.setattr(LLMConfig, "create_model", lambda _config: HangingModel())
    monkeypatch.setattr(
        "jiuwenswarm.symphony.llm._MODEL_CONNECTION_PROBE_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(BaseError) as exc_info:
        await probe_model_connection(_llm_config())

    assert exc_info.value.status is StatusCode.MODEL_CALL_FAILED
    assert str(exc_info.value) == (
        "[181001] model call failed, reason: "
        "model connection test timed out after 0.01s"
    )
    assert isinstance(exc_info.value.cause, TimeoutError)


def test_model_response_observer_preserves_orchestration_usage_context():
    reset_llm_token_usage()
    config = _llm_config()
    observer = create_model_response_observer(config)

    observer(
        SimpleNamespace(
            usage_metadata=SimpleNamespace(
                input_tokens=7,
                output_tokens=3,
                total_tokens=10,
            )
        ),
        "orchestration",
        "beam_final_rerank",
    )

    summary = get_llm_token_usage_summary()
    assert summary["by_stage"]["orchestration"]["total_tokens"] == 10
    assert (
        summary["by_operation"]["orchestration.beam_final_rerank"]["request_count"] == 1
    )
    reset_llm_token_usage()


def test_llm_identity_digest_is_complete_stable_and_redacted():
    config = LLMConfig(
        model="model-a",
        temperature=0.2,
        top_p=0.8,
        model_client_config={
            "api_key": "super-secret-api-key",
            "api_base": "https://private-endpoint.example/v1/",
            "client_provider": "openai",
            "routing": {"region": "region-a", "credential": "route-secret"},
        },
        model_config_obj={
            "max_tokens": 99,
            "extra_body": {
                "request_route": "route-a",
                "token": "request-secret",
            },
        },
    )

    reordered_config = LLMConfig(
        model="model-a",
        temperature=0.2,
        top_p=0.8,
        model_client_config={
            "routing": {"credential": "route-secret", "region": "region-a"},
            "client_provider": "openai",
            "api_base": "https://private-endpoint.example/v1/",
            "api_key": "super-secret-api-key",
        },
        model_config_obj={
            "extra_body": {
                "token": "request-secret",
                "request_route": "route-a",
            },
            "max_tokens": 99,
        },
    )
    digest = config.identity_digest()

    assert digest == reordered_config.identity_digest()
    assert digest == llm_config_signature(config)
    assert len(digest) == 64
    for sensitive_value in (
        "super-secret-api-key",
        "https://private-endpoint.example/v1",
        "route-secret",
        "request-secret",
    ):
        assert sensitive_value not in digest


@pytest.mark.parametrize(
    ("field", "updated_client", "updated_request", "updated_top_p"),
    [
        (
            "endpoint",
            {"api_base": "https://endpoint-b.example/v1"},
            {},
            0.2,
        ),
        ("provider", {"client_provider": "azure"}, {}, 0.2),
        ("routing", {"routing_region": "region-b"}, {}, 0.2),
        (
            "request",
            {},
            {"extra_body": {"request_route": "request-b"}},
            0.2,
        ),
        ("top_p", {}, {}, 0.9),
    ],
)
def test_llm_identity_digest_changes_for_every_client_affecting_setting(
    field,
    updated_client,
    updated_request,
    updated_top_p,
):
    del field
    client_config = {
        "api_key": "secret",
        "api_base": "https://endpoint-a.example/v1",
        "client_provider": "openai",
        "routing_region": "region-a",
    }
    request_config = {
        "max_tokens": 99,
        "extra_body": {"request_route": "request-a"},
    }
    baseline = LLMConfig(
        model="model-a",
        temperature=0.0,
        top_p=0.2,
        model_client_config=client_config,
        model_config_obj=request_config,
    )
    changed = LLMConfig(
        model="model-a",
        temperature=0.0,
        top_p=updated_top_p,
        model_client_config={**client_config, **updated_client},
        model_config_obj={**request_config, **updated_request},
    )

    assert baseline.identity_digest() != changed.identity_digest()


@pytest.mark.asyncio
async def test_complete_json_async_passes_request_overrides_to_invoke():
    client = create_llm_client(_llm_config())
    fake_model = _FakeInvokeModel()
    setattr(client, "_model", fake_model)

    result = await client.complete_json_async(
        system_prompt="system",
        user_content="user",
        request_overrides={
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )

    assert result == '{"ok": true}'
    assert "reasoning_effort" not in fake_model.calls[0]
    assert fake_model.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_complete_json_async_omits_request_overrides_by_default():
    client = create_llm_client(_llm_config())
    fake_model = _FakeInvokeModel()
    setattr(client, "_model", fake_model)

    await client.complete_json_async(system_prompt="system", user_content="user")

    assert "reasoning_effort" not in fake_model.calls[0]
    assert "extra_body" not in fake_model.calls[0]


@pytest.mark.asyncio
async def test_complete_json_many_async_passes_request_overrides_to_each_invoke():
    client = create_llm_client(_llm_config())
    fake_model = _FakeInvokeModel()
    setattr(client, "_model", fake_model)

    results = await client.complete_json_many_async(
        [
            {"system_prompt": "system-a", "user_content": "user-a"},
            {"system_prompt": "system-b", "user_content": "user-b"},
        ],
        request_overrides={
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )

    assert results == ['{"ok": true}', '{"ok": true}']
    assert all("reasoning_effort" not in call for call in fake_model.calls)
    assert [call["extra_body"] for call in fake_model.calls] == [
        {"thinking": {"type": "disabled"}},
        {"thinking": {"type": "disabled"}},
    ]


@pytest.mark.asyncio
async def test_complete_json_many_async_omits_request_overrides_by_default():
    client = create_llm_client(_llm_config())
    fake_model = _FakeInvokeModel()
    setattr(client, "_model", fake_model)

    await client.complete_json_many_async(
        [{"system_prompt": "system", "user_content": "user"}],
    )

    assert "reasoning_effort" not in fake_model.calls[0]
    assert "extra_body" not in fake_model.calls[0]


def test_record_usage_supports_openjiuwen_usage_metadata():
    reset_llm_token_usage()
    config = LLMConfig(
        model="model-a",
        model_client_config={
            "api_key": "key",
            "api_base": "https://example.test/v1",
            "client_provider": "openai",
        },
    )
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            input_tokens=12,
            output_tokens=5,
            total_tokens=17,
        )
    )

    try:
        _record_usage_from_response(
            config=config,
            response=response,
            operation="schema_extraction",
        )

        usage = get_llm_token_usage_summary()
        assert usage["total"]["prompt_tokens"] == 12
        assert usage["total"]["completion_tokens"] == 5
        assert usage["total"]["total_tokens"] == 17
        assert usage["records"][0]["source"] == "usage_metadata"
    finally:
        reset_llm_token_usage()
