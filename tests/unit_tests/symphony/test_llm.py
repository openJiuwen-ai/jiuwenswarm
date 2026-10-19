from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.symphony.llm import (
    LLMConfig,
    create_llm_client,
    extract_message_content,
    get_llm_token_usage_summary,
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
    response = SimpleNamespace(content=[{"text": "{\"ok\": true}"}])

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
                "model_config_obj": {"temperature": 0.2, "top_p": 0.8, "max_tokens": 99},
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


def test_llm_config_removes_internal_reasoning_level():
    config = LLMConfig.from_model_entry(
        _model_entry(reasoning_level="off", request={"max_tokens": 99})
    )

    request_kwargs = config.model_request_kwargs()

    assert "reasoning_level" not in request_kwargs
    assert request_kwargs["max_tokens"] == 99
    assert request_kwargs["extra_body"] == thinking_disabled_request_overrides()["extra_body"]


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
                "extra_body": {"custom_option": {"enabled": True}},
            },
        )
    )

    request_kwargs = config.model_request_kwargs()

    assert "reasoning_level" not in request_kwargs
    assert "reasoning_effort" not in request_kwargs
    assert request_kwargs["max_tokens"] == 99
    assert request_kwargs["extra_body"] == {
        "custom_option": {"enabled": True},
        **thinking_disabled_request_overrides()["extra_body"],
    }


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
    assert config.model_request_kwargs()["extra_body"]["custom_option"] == {"enabled": True}


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
    assert fake_model.calls[0]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }


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
    assert [
        call["extra_body"]
        for call in fake_model.calls
    ] == [
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
