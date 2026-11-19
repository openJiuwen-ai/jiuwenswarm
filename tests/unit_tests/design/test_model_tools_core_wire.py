# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer chat calls go through openjiuwen core: the wire each endpoint receives.

The real core ``Model`` is built from a Settings entry; only the vendor SDK
``create`` call is replaced, so the asserted kwargs are exactly what would go
over the network.
"""

from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest
from anthropic.resources.messages import AsyncMessages
from anthropic.types import Message
from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletion

from jiuwenswarm.common import config as config_module
from jiuwenswarm.server.runtime.designer import model_tools
from jiuwenswarm.server.runtime.designer.handlers import common as handlers_common
from jiuwenswarm.server.runtime.designer.model_tools import (
    LLM_BILLING,
    LLM_NOT_CONFIGURED,
    DesignerLlmError,
)

_LEGACY_THINKING_KEYS = {"thinking", "reasoning", "enable_thinking", "chat_template_kwargs"}


def _settings(monkeypatch: pytest.MonkeyPatch, client: dict[str, Any], mco: dict[str, Any] | None = None) -> None:
    entry = {
        "model_id": "mdl_test",
        "is_default": True,
        "model_client_config": {"api_key": "sk-test", "client_provider": "OpenAI", **client},
        "model_config_obj": dict(mco or {}),
    }
    config = {"models": {"defaults": [entry]}}
    monkeypatch.setattr(model_tools, "get_config", lambda: config)
    # interface_deep reads the config at import; keep it off the real ~/.jiuwenswarm.
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    monkeypatch.setattr(config_module, "get_endpoint_profile_overrides", lambda config=None: {})
    monkeypatch.setattr(model_tools, "_chat_billing_block", "")
    monkeypatch.setattr(model_tools, "_load_runtime_env", lambda: None)
    for key in ("API_KEY", "OPENAI_API_KEY", "API_BASE", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)


def _completion(model: str, content: str | None, reasoning_content: str | None = None) -> ChatCompletion:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return ChatCompletion.model_validate(
        {
            "id": "cmpl_test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        }
    )


class _OpenAIWire:
    """Capture chat.completions.create kwargs and answer with scripted replies."""

    def __init__(self, replies: list[ChatCompletion | BaseException]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._replies = list(replies)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_OpenAIWire":
        wire = self

        async def create(_self: AsyncCompletions, **kwargs: Any) -> ChatCompletion:
            wire.calls.append(kwargs)
            reply = wire._replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(AsyncCompletions, "create", create)
        return self

    @property
    def extra_body(self) -> dict[str, Any]:
        return dict(self.calls[-1].get("extra_body") or {})


def _status_error(status: int, message: str) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "http://gateway/v1/chat/completions"))
    return openai.APIStatusError(message, response=response, body=None)


async def _call(**kwargs: Any) -> dict[str, Any]:
    return await model_tools.call_model_tool(prompt="plan", system="director", **kwargs)


@pytest.mark.asyncio
async def test_vllm_profile_disables_thinking_with_chat_template_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(
        monkeypatch,
        {"api_base": "http://10.1.2.3:8000/v1", "model_name": "qwen3-32b", "endpoint_profile": "vllm"},
    )
    wire = _OpenAIWire([_completion("qwen3-32b", "{}")]).install(monkeypatch)

    result = await _call(max_tokens=32768)

    assert result["ok"] is True
    call = wire.calls[0]
    assert wire.extra_body == {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}}
    assert call["temperature"] == 0.4
    assert call["max_tokens"] == 32768
    assert call["timeout"] == 1200.0
    assert call["messages"][0] == {"role": "system", "content": "director"}


@pytest.mark.asyncio
async def test_bare_ip_gateway_uses_endpoint_profile_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "http://10.1.2.3:8000/v1", "model_name": "qwen3-32b"})
    monkeypatch.setattr(config_module, "get_endpoint_profile_overrides", lambda config=None: {"10.1.2.3": "vllm"})
    wire = _OpenAIWire([_completion("qwen3-32b", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.extra_body == {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}}


_VLLM_THINKING_OFF = {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_base",
    [
        "http://10.1.2.3:8000/v1",
        "http://[fd00::12]:8000/v1",
        "http://127.0.0.1:30000/v1",
        "http://localhost:8000/v1",
        "http://gpu-box:8000/v1",
        "10.1.2.3:8000/v1",
    ],
)
async def test_self_hosted_host_without_profile_gets_vllm_wire(
    monkeypatch: pytest.MonkeyPatch,
    api_base: str,
) -> None:
    _settings(monkeypatch, {"api_base": api_base, "model_name": "qwen3-32b"})
    wire = _OpenAIWire([_completion("qwen3-32b", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.extra_body == _VLLM_THINKING_OFF


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["10.20.30.40", "localhost", "llm-gateway"])
async def test_self_hosted_deepseek_disables_thinking(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    model = "DeepSeek-V4.1-Flash"
    _settings(monkeypatch, {"api_base": f"http://{host}:8000/v1", "model_name": model})
    wire = _OpenAIWire([_completion(model, "ok")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.extra_body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_generic_openai_profile_on_ip_still_gets_vllm_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(
        monkeypatch,
        {"api_base": "http://10.1.2.3:8000/v1", "model_name": "qwen3-32b", "endpoint_profile": "openai"},
    )
    wire = _OpenAIWire([_completion("qwen3-32b", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.extra_body == _VLLM_THINKING_OFF


@pytest.mark.asyncio
async def test_public_domain_is_not_treated_as_self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://llm.example.org/v1", "model_name": "qwen3-32b"})
    wire = _OpenAIWire([_completion("qwen3-32b", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert "chat_template_kwargs" not in wire.extra_body


@pytest.mark.asyncio
async def test_explicit_profile_on_ip_beats_self_hosted_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(
        monkeypatch,
        {"api_base": "http://10.1.2.3:8000/v1", "model_name": "qwen3-max", "endpoint_profile": "dashscope"},
    )
    wire = _OpenAIWire([_completion("qwen3-max", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.extra_body == {"enable_thinking": False}


@pytest.mark.parametrize(
    ("client", "overrides", "expected"),
    [
        ({"api_base": "http://10.1.2.3:8000/v1"}, {}, "vllm"),
        ({"api_base": "http://gpu-box:8000/v1", "endpoint_profile": "openai-compatible"}, {}, "vllm"),
        ({"api_base": "http://10.1.2.3:8000/v1"}, {"10.1.2.3": "dashscope"}, None),
        ({"api_base": "http://10.1.2.3:8000/v1", "endpoint_profile": "sglang"}, {}, None),
        ({"api_base": "http://10.1.2.3:8000", "client_provider": "Anthropic"}, {}, None),
        ({"api_base": "https://api.deepseek.com"}, {}, None),
        ({"api_base": ""}, {}, None),
    ],
)
def test_self_hosted_endpoint_profile_decision(
    monkeypatch: pytest.MonkeyPatch,
    client: dict[str, Any],
    overrides: dict[str, str],
    expected: str | None,
) -> None:
    monkeypatch.setattr(config_module, "get_endpoint_profile_overrides", lambda config=None: overrides)

    assert model_tools._self_hosted_endpoint_profile({"client_provider": "OpenAI", **client}) == expected


@pytest.mark.asyncio
async def test_dashscope_gets_its_own_thinking_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(
        monkeypatch,
        {"api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model_name": "qwen3-max"},
    )
    wire = _OpenAIWire([_completion("qwen3-max", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.extra_body == {"enable_thinking": False}
    assert "reasoning_effort" not in wire.calls[0]


@pytest.mark.asyncio
async def test_deepseek_gets_thinking_type_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://api.deepseek.com", "model_name": "deepseek-v4-flash"})
    wire = _OpenAIWire([_completion("deepseek-v4-flash", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.extra_body == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_model_without_off_runs_at_lowest_level_and_keeps_kimi_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, {"api_base": "https://api.moonshot.cn/v1", "model_name": "kimi-k3"})
    wire = _OpenAIWire([_completion("kimi-k3", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    call = wire.calls[0]
    assert call["reasoning_effort"] == "low"
    assert call["temperature"] == 1.0
    assert call["top_p"] == 0.95
    assert not _LEGACY_THINKING_KEYS & set(wire.extra_body)


@pytest.mark.asyncio
async def test_minimal_level_is_used_when_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://api.openai.com/v1", "model_name": "gpt-5"})
    wire = _OpenAIWire([_completion("gpt-5", "{}")]).install(monkeypatch)

    assert (await _call())["ok"] is True
    assert wire.calls[0]["reasoning_effort"] == "minimal"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_base", ["https://api.moonshot.cn/v1", "http://10.20.30.40:8000/v1"])
async def test_always_on_model_sends_no_reasoning_and_reads_reasoning_content(
    monkeypatch: pytest.MonkeyPatch, api_base: str,
) -> None:
    _settings(monkeypatch, {"api_base": api_base, "model_name": "kimi-k2-thinking"})
    wire = _OpenAIWire([_completion("kimi-k2-thinking", "", reasoning_content='{"plan": 1}')]).install(monkeypatch)

    result = await _call()

    assert result["ok"] is True
    assert result["text"] == '{"plan": 1}'
    assert "reasoning_effort" not in wire.calls[0]
    assert not _LEGACY_THINKING_KEYS & set(wire.extra_body)


@pytest.mark.asyncio
async def test_empty_reply_retries_once_at_lower_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://api.deepseek.com", "model_name": "deepseek-v4-flash"})
    wire = _OpenAIWire(
        [_completion("deepseek-v4-flash", ""), _completion("deepseek-v4-flash", "second")]
    ).install(monkeypatch)

    result = await _call()

    assert result["text"] == "second"
    assert [call["temperature"] for call in wire.calls] == [0.4, 0.2]


@pytest.mark.asyncio
async def test_rejected_reasoning_knob_retries_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://llm.example.org/v1", "model_name": "my-local-model"})
    wire = _OpenAIWire(
        [_status_error(400, "reasoning_effort: Input should be 'low', 'medium' or 'high'"), _completion("m", "ok")]
    ).install(monkeypatch)

    result = await _call()

    assert result["ok"] is True
    assert wire.calls[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in wire.calls[1]


@pytest.mark.asyncio
async def test_402_from_core_sets_billing_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://api.deepseek.com", "model_name": "deepseek-v4-flash"})
    _OpenAIWire([_status_error(402, "Insufficient Balance")]).install(monkeypatch)

    try:
        result = await _call()
        assert result["ok"] is False
        assert result["unavailable"] is True
        assert result["code"] == LLM_BILLING
        assert model_tools.chat_model_billing_block()
    finally:
        model_tools._chat_billing_block = ""


@pytest.mark.asyncio
async def test_anthropic_protocol_goes_through_messages_api(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(
        monkeypatch,
        {
            "api_base": "https://api.anthropic.com",
            "model_name": "claude-sonnet-4-5",
            "client_provider": "Anthropic",
        },
    )
    calls: list[dict[str, Any]] = []

    async def create(_self: AsyncMessages, **kwargs: Any) -> Message:
        calls.append(kwargs)
        return Message.model_validate(
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "brief"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 5},
            }
        )

    monkeypatch.setattr(AsyncMessages, "create", create)

    result = await model_tools.call_model_tool(
        prompt="describe",
        system="director",
        images=["data:image/png;base64,iVBORw0KGgo="],
    )

    assert result["ok"] is True
    assert result["text"] == "brief"
    call = calls[0]
    assert call["thinking"] == {"type": "disabled"}
    assert "director" in str(call["system"])
    user_blocks = call["messages"][-1]["content"]
    assert [block["type"] for block in user_blocks] == ["text", "image"]


@pytest.mark.asyncio
async def test_preferred_model_selects_its_own_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {
            "is_default": True,
            "model_client_config": {
                "api_key": "sk-a",
                "api_base": "https://api.deepseek.com",
                "model_name": "deepseek-v4-flash",
                "client_provider": "OpenAI",
            },
        },
        {
            "model_client_config": {
                "api_key": "sk-b",
                "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model_name": "qwen3-max",
                "client_provider": "OpenAI",
            },
        },
    ]
    monkeypatch.setattr(model_tools, "get_config", lambda: {"models": {"defaults": entries}})
    monkeypatch.setattr(config_module, "get_endpoint_profile_overrides", lambda config=None: {})
    monkeypatch.setattr(model_tools, "_chat_billing_block", "")
    wire = _OpenAIWire([_completion("qwen3-max", "{}")]).install(monkeypatch)

    with model_tools.use_preferred_designer_model("qwen3-max"):
        result = await _call()

    assert result["model_name"] == "qwen3-max"
    assert wire.calls[0]["model"] == "qwen3-max"
    assert wire.extra_body == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_complete_designer_text_shares_the_designer_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://api.deepseek.com", "model_name": "deepseek-v4-flash"})
    wire = _OpenAIWire([_completion("deepseek-v4-flash", "# Brief")]).install(monkeypatch)

    text = await handlers_common.complete_designer_text("write a brief", max_tokens=2048)

    assert text == "# Brief"
    call = wire.calls[0]
    assert call["messages"] == [{"role": "user", "content": "write a brief"}]
    assert call["max_tokens"] == 2048
    assert wire.extra_body == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_complete_designer_text_fails_closed_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, {"api_base": "https://api.deepseek.com", "model_name": "deepseek-v4-flash", "api_key": ""})

    with pytest.raises(DesignerLlmError) as excinfo:
        await handlers_common.complete_designer_text("write a brief")
    assert excinfo.value.code == LLM_NOT_CONFIGURED
