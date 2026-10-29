# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Script analysis timeout reporting and the Director thinking switch."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.designer import model_tools, script_analysis
from jiuwenswarm.server.runtime.designer.model_tools import (
    DesignerLlmError,
    _thinking_disabled_extra_body,
)


@pytest.mark.asyncio
async def test_script_analysis_timeout_reports_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        raise asyncio.TimeoutError

    monkeypatch.setattr(model_tools, "call_model_tool", fake_model_call)

    with pytest.raises(DesignerLlmError) as excinfo:
        await script_analysis.analyze_creative_brief("A short film.", timeout_sec=5)

    message = str(excinfo.value)
    assert "during script analysis (no reply arrived in time)" in message
    assert "after 120s" not in message


@pytest.mark.asyncio
async def test_script_analysis_timeout_reports_reinforce_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        calls.append(True)
        if len(calls) == 1:
            return {"ok": True, "text": "not a json object"}
        raise asyncio.TimeoutError

    monkeypatch.setattr(model_tools, "call_model_tool", fake_model_call)

    with pytest.raises(DesignerLlmError) as excinfo:
        await script_analysis.analyze_creative_brief("A short film.", timeout_sec=5)

    assert len(calls) == 2
    assert "the first reply was not usable JSON and the retry did not finish in time" in str(excinfo.value)


@pytest.mark.parametrize(
    "api_base",
    [
        "http://10.20.30.40:8000/v1",
        "http://localhost:8000/v1",
        "http://llm-gateway:8000/v1",
    ],
)
def test_self_hosted_endpoint_keeps_provider_neutral_body(api_base: str) -> None:
    body = _thinking_disabled_extra_body(api_base, "DeepSeek-V4.1-Flash")

    assert body == {
        "thinking": {"type": "disabled"},
        "reasoning": {"enabled": False},
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_self_hosted_endpoint_skips_models_that_cannot_disable_thinking() -> None:
    assert _thinking_disabled_extra_body("http://10.20.30.40:8000/v1", "kimi-k2-thinking") == {}


def test_explicit_vllm_profile_gets_chat_template_switch() -> None:
    body = _thinking_disabled_extra_body("https://llm.example.com/v1", "qwen3-32b", "vllm")

    assert body == {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}}


def test_public_generic_endpoint_keeps_single_switch() -> None:
    body = _thinking_disabled_extra_body("https://api.deepseek.com/v1", "deepseek-chat")

    assert body == {"thinking": {"type": "disabled"}}


def test_dashscope_endpoint_keeps_enable_thinking_only() -> None:
    body = _thinking_disabled_extra_body(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3-235b-a22b",
    )

    assert "chat_template_kwargs" not in body
