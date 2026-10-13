from types import SimpleNamespace

from jiuwenswarm.common import reasoning_injector
from jiuwenswarm.llm_provider_compat_patch import (
    _coalesce_tool_call_fragments,
    _drop_orphan_tool_calls,
    _patch_anthropic_modelarts,
    _patch_openai_modelarts_tool_choice,
)


class _FakeAnthropicClient:
    def __init__(self, api_base, model="openpangu-2.0-pro", thinking=None):
        self.model_client_config = SimpleNamespace(api_base=api_base)
        self.model = model
        self.thinking = thinking

    def _build_anthropic_params(self, *args, **kwargs):
        del args, kwargs
        params = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {"role": "user", "content": [{"type": "image", "source": {}}]},
            ],
            "system": [{"type": "text", "text": "system"}],
        }
        if self.thinking is not None:
            params["thinking"] = self.thinking
        return params


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
    params = _FakeAnthropicClient("https://api.modelarts-maas.com/anthropic/v1")._build_anthropic_params()

    assert params["messages"][0]["content"] == "hello"
    assert params["messages"][1]["content"][0]["type"] == "image"
    assert params["system"] == "system"
    assert params["thinking"] == {"type": "disabled"}


def test_non_modelarts_anthropic_preserves_standard_blocks():
    _patch_anthropic_modelarts(_FakeAnthropicClient)
    params = _FakeAnthropicClient("https://api.anthropic.com")._build_anthropic_params()

    assert params["messages"][0]["content"] == [{"type": "text", "text": "hello"}]
    assert "thinking" not in params


def test_modelarts_anthropic_preserves_explicit_thinking():
    _patch_anthropic_modelarts(_FakeAnthropicClient)
    params = _FakeAnthropicClient(
        "https://api.modelarts-maas.com/anthropic/v1",
        thinking={"type": "enabled", "budget_tokens": 2048},
    )._build_anthropic_params()

    assert params["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_modelarts_non_pangu_keeps_provider_thinking_default():
    _patch_anthropic_modelarts(_FakeAnthropicClient)
    params = _FakeAnthropicClient(
        "https://api.modelarts-maas.com/anthropic/v1",
        model="glm-5.2",
    )._build_anthropic_params()

    assert "thinking" not in params


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


def test_first_chunk_tool_call_fragments_merge_like_later_chunks():
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

    def _call(call_id, name, arguments, index=0):
        return ToolCall(id=call_id, type="function", name=name, arguments=arguments, index=index)

    def _calls(*tool_calls):
        chunk = AssistantMessageChunk(content="", tool_calls=list(tool_calls))
        return [(tc.id, tc.name, tc.arguments) for tc in _coalesce_tool_call_fragments(chunk).tool_calls]

    assert _calls(_call("call_1", "view_task", ""), _call("", "", "{}")) == [("call_1", "view_task", "{}")]
    tail_then_new = _calls(_call("", "", "}"), _call("call_2", "ping", "", index=1))
    assert tail_then_new == [("", "", "}"), ("call_2", "ping", "")]
    parallel = _calls(_call("call_1", "a", "{}"), _call("call_2", "b", "{}", index=1))
    assert parallel == [("call_1", "a", "{}"), ("call_2", "b", "{}")]


def test_history_tool_calls_without_id_and_name_are_dropped_before_sending():
    good = {"id": "call_1", "type": "function", "function": {"name": "view_task", "arguments": ""}}
    orphan = {"id": "", "type": "function", "function": {"name": "", "arguments": "{}"}}
    history = [{"role": "assistant", "content": "", "tool_calls": [good, orphan]}]

    sent = _drop_orphan_tool_calls(history)

    assert sent[0]["tool_calls"] == [good]
    assert history[0]["tool_calls"] == [good, orphan]
    assert _drop_orphan_tool_calls(sent) is sent
