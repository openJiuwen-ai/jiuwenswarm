from types import SimpleNamespace

from jiuwenswarm.llm_provider_compat_patch import (
    _patch_anthropic_modelarts,
    _patch_openai_modelarts_tool_choice,
)


def _client_class(params, api_base="https://api.modelarts-maas.com/v1"):
    class FakeClient:
        def __init__(self):
            self.model_client_config = SimpleNamespace(api_base=api_base)

        def _build_request_params(self, *args, **kwargs):
            return params

    return FakeClient


def test_modelarts_anthropic_flattens_only_pure_text_blocks():
    text = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    image = [{"type": "image", "source": {"data": "..."}}]
    client_class = _client_class(
        {"messages": [{"content": text}, {"content": image}], "system": text}
    )

    _patch_anthropic_modelarts(client_class)
    result = client_class()._build_request_params()

    assert result["messages"][0]["content"] == "hello\nworld"
    assert result["messages"][1]["content"] == image
    assert result["system"] == "hello\nworld"


def test_non_modelarts_anthropic_keeps_content_blocks():
    content = [{"type": "text", "text": "hello"}]
    client_class = _client_class(
        {"messages": [{"content": content}]}, "https://api.anthropic.com/v1"
    )

    _patch_anthropic_modelarts(client_class)

    assert client_class()._build_request_params()["messages"][0]["content"] == content


def test_modelarts_qwen_disables_unsupported_auto_tool_choice():
    client_class = _client_class(
        {"model": "qwen3-32b", "tools": [{"type": "function"}], "tool_choice": "auto"}
    )

    _patch_openai_modelarts_tool_choice(client_class)

    assert client_class()._build_request_params()["tool_choice"] == "none"


def test_other_openai_model_or_host_keeps_auto_tool_choice():
    params = {"model": "other-model", "tools": [{}], "tool_choice": "auto"}
    modelarts_client = _client_class(params.copy())
    other_client = _client_class(
        {"model": "qwen3-32b", "tools": [{}], "tool_choice": "auto"},
        "https://api.openai.com/v1",
    )

    _patch_openai_modelarts_tool_choice(modelarts_client)
    _patch_openai_modelarts_tool_choice(other_client)

    assert modelarts_client()._build_request_params()["tool_choice"] == "auto"
    assert other_client()._build_request_params()["tool_choice"] == "auto"
