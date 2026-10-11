# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""聊天前的三步准备：钩子、抄团队名字、隐式反馈。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_KV_CACHE_MOD = "openjiuwen.core.kv_cache"
if _KV_CACHE_MOD not in sys.modules:
    try:
        __import__(_KV_CACHE_MOD)
    except ModuleNotFoundError:
        _stub = types.ModuleType(_KV_CACHE_MOD)
        _stub.KVCacheAffinityConfig = type("KVCacheAffinityConfig", (), {})
        sys.modules[_KV_CACHE_MOD] = _stub

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.middleware.chat import (
    ensure_auto_team_binding,
    should_trigger_before_chat_request,
    trigger_before_chat_request,
    try_record_implicit_feedback,
)

_SERVER_FILE = (
    Path(__file__).resolve().parents[3] / "jiuwenswarm" / "server" / "agent_ws_server.py"
)


def _request(method: ReqMethod, **kwargs) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="web",
        req_method=method,
        **kwargs,
    )


def test_hook_recognizes_only_chat_methods() -> None:
    assert should_trigger_before_chat_request(_request(ReqMethod.CHAT_SEND))
    assert should_trigger_before_chat_request(_request(ReqMethod.CHAT_RESUME))
    assert should_trigger_before_chat_request(_request(ReqMethod.CHAT_ANSWER))
    assert not should_trigger_before_chat_request(_request(ReqMethod.SESSION_LIST))


@pytest.mark.asyncio
async def test_non_chat_hook_returns_without_calling_extensions(monkeypatch) -> None:
    registry = types.ModuleType("jiuwenswarm.extensions.registry")

    class _Registry:
        @staticmethod
        def get_instance() -> None:
            raise AssertionError("非聊天请求不应调用扩展")

    registry.ExtensionRegistry = _Registry
    monkeypatch.setitem(sys.modules, registry.__name__, registry)
    await trigger_before_chat_request(_request(ReqMethod.SESSION_LIST))


@pytest.mark.asyncio
async def test_chat_hook_passes_params_to_extensions(monkeypatch) -> None:
    seen: list[object] = []

    class _Registry:
        @staticmethod
        def get_instance() -> _Registry:
            return _Registry()

        async def trigger(self, event, ctx) -> None:
            seen.append((event, ctx.params))

    registry = types.ModuleType("jiuwenswarm.extensions.registry")
    registry.ExtensionRegistry = _Registry
    monkeypatch.setitem(sys.modules, "jiuwenswarm.extensions.registry", registry)

    request = _request(ReqMethod.CHAT_SEND, session_id="sess-1", params={"query": "你好"})
    await trigger_before_chat_request(request)

    assert len(seen) == 1
    assert seen[0][1]["query"] == "你好"


@pytest.mark.asyncio
async def test_existing_team_name_is_copied_onto_chat_send(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id, cache_bust=True: {
            "mode": "team",
            "team_name": "legacy_team",
            "team_template_id": "legacy_template",
        },
    )
    request = _request(
        ReqMethod.CHAT_SEND,
        session_id="sess-legacy-team",
        params={"mode": "team", "query": "继续"},
    )

    result = await ensure_auto_team_binding(request)

    assert result == "legacy_team"
    assert request.params["team_name"] == "legacy_team"
    assert request.params["team_template_id"] == "legacy_template"


@pytest.mark.asyncio
async def test_non_chat_does_not_copy_team_name() -> None:
    request = _request(
        ReqMethod.SESSION_LIST,
        session_id="sess-legacy-team",
        params={"mode": "team"},
    )
    assert await ensure_auto_team_binding(request) is None
    assert "team_name" not in request.params


@pytest.mark.asyncio
async def test_implicit_feedback_skips_proactive_and_non_chat(monkeypatch) -> None:
    collector = types.ModuleType(
        "jiuwenswarm.agents.harness.common.recommendation.feedback_collector"
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("这条请求不该记隐式反馈")

    collector.find_latest_recommendation = fail
    collector.record_implicit_feedback = fail
    monkeypatch.setitem(sys.modules, collector.__name__, collector)

    proactive = _request(
        ReqMethod.CHAT_SEND,
        session_id="sess-1",
        params={"query": "系统指令", "source": "proactive_recommendation"},
    )
    await try_record_implicit_feedback(proactive)
    await try_record_implicit_feedback(_request(ReqMethod.CHAT_RESUME, session_id="sess-1"))


@pytest.mark.asyncio
async def test_implicit_feedback_records_the_following_reply(monkeypatch) -> None:
    recorded: list[tuple[str, str]] = []
    collector = types.ModuleType(
        "jiuwenswarm.agents.harness.common.recommendation.feedback_collector"
    )
    collector.find_latest_recommendation = lambda session_id, max_age_seconds=0: {"id": "rec-1"}
    collector.record_implicit_feedback = lambda rec_id, query: recorded.append((rec_id, query))
    monkeypatch.setitem(sys.modules, collector.__name__, collector)

    await try_record_implicit_feedback(
        _request(ReqMethod.CHAT_SEND, session_id="sess-1", params={"query": "简洁点"})
    )

    assert recorded == [("rec-1", "简洁点")]


def test_websocket_still_calls_the_three_steps_on_the_server() -> None:
    text = _SERVER_FILE.read_text(encoding="utf-8")
    assert "await self._trigger_before_chat_request_hook(request)" in text
    assert "await self._ensure_auto_team_binding_for_chat(request)" in text
    assert "await self._try_record_implicit_feedback(request)" in text
    assert "await trigger_before_chat_request(request)" in text
    assert "return await ensure_auto_team_binding(request)" in text
    assert "await try_record_implicit_feedback(request)" in text
