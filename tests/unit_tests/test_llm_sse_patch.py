# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SSE gateway failures must not be assembled into successful empty replies."""

import json

import pytest

from jiuwenswarm.llm_sse_patch import assemble_openai_response


def _data(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def _text(text):
    return {"choices": [{"message": {"token_text": text}, "finish_reason": "stop"}]}


@pytest.mark.parametrize("prefix", ["", "id: first\n" + _data(_text("partial"))])
@pytest.mark.parametrize("suffix", ["", "data: [DONE]\n\n", _data(_text("later"))])
def test_upstream_error_is_raised_even_after_partial_content(prefix, suffix):
    response = (
        prefix
        + _data(
            {
                "error": {
                    "type": "server_error",
                    "code": "upstream_failure",
                    "message": "gateway unavailable",
                }
            }
        )
        + suffix
    )
    with pytest.raises(
        Exception, match="server_error.*upstream_failure.*gateway unavailable"
    ):
        assemble_openai_response(response)


def test_existing_gateway_text_assembly_is_preserved():
    chunks = [_text("hello "), _text("world")]
    response = "".join(f"id: {i}\n" + _data(chunk) for i, chunk in enumerate(chunks))
    completion = assemble_openai_response(response)
    assert completion.choices[0].message.content == "hello world"
    assert completion.choices[0].finish_reason == "stop"


def test_tool_calls_without_text_remain_valid():
    response = _data(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"test.txt"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    completion = assemble_openai_response(response)
    choice = completion.choices[0]
    assert choice.message.content is None
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls[0].function.name == "read_file"
    assert choice.message.tool_calls[0].function.arguments == '{"path":"test.txt"}'


def test_gateway_reasoning_and_usage_are_preserved():
    response = _data(
        {
            "choices": [
                {
                    "message": {
                        "reasoning_token_text": "reasoning",
                        "token_text": "answer",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            },
        }
    )
    completion = assemble_openai_response(response)
    assert completion.choices[0].message.content == "answer"
    assert completion.choices[0].message.reasoning_content == "reasoning"
    assert completion.usage.total_tokens == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("sse_error", [False, True])
async def test_http_200_error_reaches_model_call_failure(monkeypatch, sse_error):
    """Exercise the real SDK/client boundary with an in-memory HTTP transport."""
    import httpx
    from openai import AsyncOpenAI
    from openjiuwen.core.common.exception.codes import StatusCode
    from openjiuwen.core.common.exception.errors import BaseError
    from openjiuwen.core.foundation.llm import (
        ModelClientConfig,
        ModelRequestConfig,
        UserMessage,
    )
    from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
        OpenAIModelClient,
    )

    import jiuwenswarm.llm_sse_patch as patch

    monkeypatch.setattr(patch, "_PATCH_APPLIED", False)
    monkeypatch.setattr(
        OpenAIModelClient, "_sse_invoke_patch_applied", False, raising=False
    )
    # Record the attribute so pytest restores it after the runtime patch.
    monkeypatch.setattr(
        OpenAIModelClient, "_parse_response", OpenAIModelClient._parse_response
    )
    patch.apply_openai_sse_invoke_patch()

    def respond(request):
        if sse_error:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=_data(
                    {
                        "error": {
                            "type": "server_error",
                            "code": "upstream_failure",
                            "message": "gateway unavailable",
                        }
                    }
                )
                + "data: [DONE]\n\n",
            )
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "created": 1,
                "model": "test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    config = ModelClientConfig(
        client_provider="OpenAI",
        api_key="test-key",
        api_base="http://test.invalid/v1",
    )
    client = OpenAIModelClient(
        model_client_config=config,
        model_config=ModelRequestConfig(model="test"),
    )
    async with AsyncOpenAI(
        api_key="test-key",
        base_url=config.api_base,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(
            client, "_create_async_openai_client", lambda timeout=None: sdk
        )
        if sse_error:
            with pytest.raises(
                BaseError, match="server_error.*upstream_failure.*gateway unavailable"
            ) as caught:
                await client.invoke([UserMessage(content="test")])
            assert caught.value.status == StatusCode.MODEL_CALL_FAILED
        else:
            result = await client.invoke([UserMessage(content="test")])
            assert result.content == "answer"
