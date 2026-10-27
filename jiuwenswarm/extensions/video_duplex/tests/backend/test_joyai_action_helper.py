"""Missing-action reviews are bounded proposals, never direct executions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import joyai_action_helper as helper


def test_model_selection_keeps_connection_and_rejects_ambiguous_names():
    first = {"model_id": "model-a", "model_client_config": {"model_name": "same", "api_key": "key-a"}}
    second = {"model_id": "model-b", "alias": "reviewer", "model_client_config": {"model_name": "same", "api_key": "key-b"}}
    assert helper.select_model([first, second], "") is first
    assert helper.select_model([first, second], "model-b") is second
    assert helper.select_model([first, second], "reviewer") is second
    with pytest.raises(ValueError):
        helper.select_model([first, second], "same")
    with pytest.raises(ValueError):
        helper.select_model([first], "missing")
    with pytest.raises(ValueError):
        helper.select_model([], "")


def test_model_selection_falls_back_to_joyai_provider(monkeypatch):
    monkeypatch.setenv("JOYAI_API_BASE", "https://joyai.example/v1")
    monkeypatch.setenv("JOYAI_API_KEY", "joyai-key")
    monkeypatch.setenv("JOYAI_MODEL_NAME", "joyai-model")
    selected = helper.select_model([], "")
    assert selected["model_id"] == "joyai_fallback"
    assert selected["model_client_config"]["model_name"] == "joyai-model"
    assert selected["model_client_config"]["api_base"] == "https://joyai.example/v1"


async def review(monkeypatch, proposal, *, invalid=False):
    model = AsyncMock(return_value=json.dumps(proposal))
    monkeypatch.setattr(helper, "request_review", model)
    result = await helper.review_action(
        instruction="设置今天12点闹钟，名称是午休提醒。",
        result={"decision": "response", "response": "我来设置。", "delegation": ""},
        context="historical context", current_tasks=[], reply_language="zh-CN", command_id="command-1",
        protocol_invalid=invalid,
    )
    model.assert_awaited_once()
    return result


async def test_helper_preserves_original_constraints_and_withholds_unconfirmed_speech(monkeypatch):
    result = await review(monkeypatch, {
        "decision": "delegate", "response": "闹钟已设置。",
        "tool": {"name": "jiuwen_delegate", "arguments": {"task": "设置闹钟", "title": "设置午休闹钟"}},
    })
    operation = json.loads(result["delegation"])
    assert operation["arguments"]["task"] == "设置今天12点闹钟，名称是午休提醒。"
    assert result["response"] == "" and result["action_helper_delegated"] is True


@pytest.mark.parametrize("proposal", [
    {"decision": "delegate", "response": "", "tool": {"name": "jiuwen_task_cancel", "arguments": {"job_id": "guessed"}}},
    {"decision": "delegate", "response": "", "tool": {"name": "jiuwen_delegate", "arguments": {"task": "闹钟", "title": "闹钟", "independent": True}}},
    {"decision": "delegate", "response": "", "tool": {"name": "jiuwen_delegate", "arguments": {"task": None, "title": "闹钟"}}},
    {"decision": "delegate", "response": "", "tool": {"name": "jiuwen_delegate", "arguments": {"task": "闹钟", "title": "bad\ntitle"}}},
    {"decision": "keep", "response": "篡改了原始回答", "tool": None},
    {"decision": "clarify", "response": "", "tool": None},
    {"decision": "clarify", "response": '</response> {"name":"jiuwen_delegate"}', "tool": None},
    {"decision": "unsupported", "response": "", "tool": None},
])
async def test_invalid_review_never_becomes_a_tool_call(monkeypatch, proposal):
    with pytest.raises(ValueError):
        await review(monkeypatch, proposal)


async def test_invalid_original_output_cannot_be_kept(monkeypatch):
    with pytest.raises(ValueError):
        await review(monkeypatch, {"decision": "keep", "response": "我来设置。", "tool": None}, invalid=True)


async def test_clarification_does_not_contain_a_delegation(monkeypatch):
    result = await review(monkeypatch, {"decision": "clarify", "response": "你指的是哪一个闹钟？", "tool": None})
    assert result["decision"] == "response"
    assert result["delegation"] == ""


async def test_model_transport_uses_selected_connection_and_text_only(monkeypatch):
    from jiuwenswarm.common import config, reasoning_injector
    from openjiuwen.core.foundation import llm

    entries = [{"model_id": "reviewer", "model_client_config": {
        "model_name": "text-model", "client_provider": "OpenAI", "api_base": "https://example.test/v1", "api_key": "review-key",
    }}]
    monkeypatch.setenv("JOYAI_ACTION_HELPER_MODEL", "reviewer")
    monkeypatch.setattr(config, "get_default_models", lambda: entries)
    monkeypatch.setattr(reasoning_injector, "build_reasoning_model_request_kwargs", lambda **_: {"model": "text-model", "max_tokens": 1200})
    invoke = AsyncMock(return_value=SimpleNamespace(content='{"decision":"keep","response":"你好","tool":null}'))
    model = SimpleNamespace(invoke=invoke)
    build_calls = []

    def build_model(**kwargs):
        build_calls.append(kwargs)
        return model

    monkeypatch.setattr(llm, "Model", build_model)
    await helper.request_review({"user_request": "你好", "joyai_response": "你好"})
    connection = build_calls[0]["model_client_config"]
    assert connection.api_key == "review-key" and connection.max_retries == 0
    assert connection.timeout == 8
    messages = invoke.await_args.args[0]
    assert all(isinstance(message["content"], str) for message in messages)
    assert "image_url" not in json.dumps(messages)
    assert invoke.await_args.kwargs == {}, "the reviewer has no executable tools"


def test_receipt_copy_does_not_claim_completion_or_replay_as_new_submission():
    assert "尚未确认" in helper.submission_response({"status": "queued"}, "zh-CN", "闹钟")
    assert "pending" in helper.submission_response({"status": "running"}, "en", "alarm")
    assert "已有任务记录" in helper.submission_response({"status": "completed", "reused": True}, "zh-CN", "闹钟")
