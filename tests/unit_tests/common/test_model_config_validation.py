import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.model_config_validation import (
    is_placeholder_api_base,
    is_placeholder_model_entry,
    model_client_config_view,
    probe_model_connection,
)


def test_is_placeholder_api_base_detects_documentation_domains():
    assert is_placeholder_api_base("https://example.com/compatible-mode/v1")
    assert is_placeholder_api_base("https://api.example.com/v1")
    assert is_placeholder_api_base("https://example.org")
    assert is_placeholder_api_base("https://docs.example.net/v1")


def test_is_placeholder_api_base_allows_real_domains_and_empty_values():
    assert not is_placeholder_api_base("https://real.provider.test/v1")
    assert not is_placeholder_api_base("https://example.test/v1")
    assert not is_placeholder_api_base("")


def test_is_placeholder_model_entry_detects_first_run_credentials():
    # 首次启动：.env.template 复制来的占位 API_BASE
    assert is_placeholder_model_entry({"api_base": "https://example.com/compatible-mode/v1"})
    # 仅占位 model_name / api_key 也应命中
    assert is_placeholder_model_entry({"model_name": "your-model-name"})
    assert is_placeholder_model_entry({"api_key": "sk-xxxxxxxxx"})
    assert not is_placeholder_model_entry(None)


def test_is_placeholder_model_entry_allows_real_credentials():
    # 本地 vLLM：api_base 为真实地址、无 key → 不算占位
    assert not is_placeholder_model_entry({
        "api_base": "http://127.0.0.1:8000/v1",
        "api_key": "",
        "model_name": "local-model",
    })
    assert not is_placeholder_model_entry({
        "api_base": "https://api.provider.test/v1",
        "api_key": "sk-real-key",
        "model_name": "gpt-4",
    })


def test_model_client_config_view_normalizes_dict_and_object():
    obj = SimpleNamespace(api_base="https://x.test/v1", api_key="k", model_name="m")
    assert model_client_config_view(obj) == {"api_base": "https://x.test/v1", "api_key": "k", "model_name": "m"}
    assert model_client_config_view({"api_base": None, "api_key": None, "model_name": None}) == {
        "api_base": "",
        "api_key": "",
        "model_name": "",
    }
    # dict 与等价对象判定结果一致
    assert is_placeholder_model_entry(model_client_config_view(obj)) == is_placeholder_model_entry(
        {"api_base": obj.api_base, "api_key": obj.api_key, "model_name": obj.model_name}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["exception", "empty"])
async def test_probe_model_connection_retries_with_supremum_tokens(first_failure):
    calls = []

    class FakeModel:
        async def invoke(self, messages, **kwargs):
            calls.append((messages, kwargs))
            if len(calls) == 1:
                if first_failure == "exception":
                    raise RuntimeError("token budget too small")
                return {"content": "", "reasoning_content": ""}
            return {"content": "hello"}

    result = await probe_model_connection(
        FakeModel(),
        token_limits=(3, 16),
        invoke_kwargs={"temperature": 0},
    )

    assert [kwargs["max_tokens"] for _, kwargs in calls] == [3, 16]
    assert all(kwargs["temperature"] == 0 for _, kwargs in calls)
    assert result.content == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"content": "", "reasoning_content": "thinking"},
        {"content": "", "usage_metadata": {"output_tokens": 3}},
        SimpleNamespace(
            content="",
            reasoning_content="",
            usage_metadata=SimpleNamespace(output_tokens=3),
        ),
    ],
)
async def test_probe_model_connection_accepts_reasoning_or_generated_tokens(response):
    calls = []

    class FakeModel:
        async def invoke(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return response

    result = await probe_model_connection(FakeModel())

    assert len(calls) == 1
    assert result.response is response


@pytest.mark.asyncio
async def test_probe_model_connection_enforces_invocation_deadline():
    class HangingModel:
        async def invoke(self, messages, **kwargs):
            del messages, kwargs
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await probe_model_connection(
            HangingModel(),
            token_limits=(16,),
            timeout_seconds=0.01,
        )
