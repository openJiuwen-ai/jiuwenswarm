from types import SimpleNamespace

from jiuwenswarm.common import reasoning_injector
from jiuwenswarm.llm_provider_compat_patch import (
    _patch_anthropic_modelarts,
    _patch_openai_modelarts_tool_choice,
)


class _FakeAnthropicClient:
    def __init__(self, api_base):
        self.model_client_config = SimpleNamespace(api_base=api_base)

    def _build_request_params(self, *args, **kwargs):
        del args, kwargs
        return {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {"role": "user", "content": [{"type": "image", "source": {}}]},
            ],
            "system": [{"type": "text", "text": "system"}],
        }


class _FakeOpenAIClient:
    def __init__(self, api_base, model="qwen3-32b"):
        self.model_client_config = SimpleNamespace(api_base=api_base)
        self.model = model

    def _build_request_params(self, *args, **kwargs):
        del args, kwargs
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "ping"}}],
            "tool_choice": "auto",
        }


def test_modelarts_anthropic_flattens_only_pure_text_blocks():
    _patch_anthropic_modelarts(_FakeAnthropicClient)
    params = _FakeAnthropicClient("https://api.modelarts-maas.com/anthropic/v1")._build_request_params()

    assert params["messages"][0]["content"] == "hello"
    assert params["messages"][1]["content"][0]["type"] == "image"
    assert params["system"] == "system"


def test_non_modelarts_anthropic_preserves_standard_blocks():
    _patch_anthropic_modelarts(_FakeAnthropicClient)
    params = _FakeAnthropicClient("https://api.anthropic.com")._build_request_params()

    assert params["messages"][0]["content"] == [{"type": "text", "text": "hello"}]


def test_modelarts_qwen_disables_unsupported_auto_tool_choice():
    _patch_openai_modelarts_tool_choice(_FakeOpenAIClient)
    params = _FakeOpenAIClient("https://api.modelarts-maas.com/openai/v1")._build_request_params()

    assert params["tool_choice"] == "none"


def test_modelarts_qwen30b_a3b_disables_unsupported_auto_tool_choice():
    _patch_openai_modelarts_tool_choice(_FakeOpenAIClient)
    params = _FakeOpenAIClient(
        "https://api.modelarts-maas.com/openai/v1",
        model="qwen3-30b-a3b",
    )._build_request_params()

    assert params["tool_choice"] == "none"


def test_other_models_and_hosts_keep_auto_tool_choice():
    _patch_openai_modelarts_tool_choice(_FakeOpenAIClient)

    other_model = _FakeOpenAIClient(
        "https://api.modelarts-maas.com/openai/v1", model="glm-5.2"
    )._build_request_params()
    other_host = _FakeOpenAIClient(
        "https://api.openai.com/v1", model="qwen3-32b"
    )._build_request_params()

    assert other_model["tool_choice"] == "auto"
    assert other_host["tool_choice"] == "auto"


def test_shared_model_builder_installs_provider_patches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        reasoning_injector,
        "apply_provider_compat_patches",
        lambda: calls.append(True),
    )

    result = reasoning_injector.build_reasoning_model_request_kwargs(
        model_client_config={"client_provider": "OpenAI"},
        model_config_obj={},
        model_name="qwen3-32b",
    )

    assert calls == [True]
    assert result["model"] == "qwen3-32b"
