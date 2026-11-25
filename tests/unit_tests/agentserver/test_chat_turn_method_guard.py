# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""枚举内未接 handler 的方法不得落进聊天执行链路。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.method_guard import (
    CHAT_TURN_METHOD_VALUES,
    is_chat_turn_method,
    method_not_found_payload,
    method_not_found_response,
    method_not_found_stream_payload,
    req_method_value,
)

INTERFACE = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm"
    / "server"
    / "runtime"
    / "agent_adapter"
    / "interface.py"
)

# skilldev 在流式入口有独立分支，不属于这组共用清单。
STATELESS_HANDLERS = (
    "_handle_skills_request",
    "_handle_session_asset_request",
    "_handle_assets_package_request",
    "_handle_plugins_request",
    "_handle_package_catalog_request",
)


@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.CHAT_SEND,
        ReqMethod.CHAT_RESUME,
        ReqMethod.CHAT_ANSWER,
        ReqMethod.COMMAND_GOAL,
    ],
)
def test_chat_entry_methods_may_fall_through(method: ReqMethod) -> None:
    assert is_chat_turn_method(method) is True
    assert is_chat_turn_method(method.value) is True


@pytest.mark.parametrize(
    "method",
    [ReqMethod.FILES_LIST, ReqMethod.SKILLS_LIST, ReqMethod.TEAM_CREATE],
)
def test_other_known_methods_may_not_fall_through(method: ReqMethod) -> None:
    assert is_chat_turn_method(method) is False
    assert is_chat_turn_method(method.value) is False


def test_internal_requests_without_method_still_fall_through() -> None:
    assert is_chat_turn_method(None) is True


def test_unknown_string_is_not_a_chat_turn() -> None:
    assert is_chat_turn_method("future.extension") is False


def test_chat_turn_allowlist_is_exactly_the_chat_entries() -> None:
    assert CHAT_TURN_METHOD_VALUES == {
        "chat.send",
        "chat.resume",
        "chat.user_answer",
        "command.goal",
    }


def test_chat_turn_allowlist_covers_runtime_chat_turn_methods() -> None:
    """runtime 认定的聊天轮次方法都必须能通过兜底，否则流式应答会被误拒。"""
    from jiuwenswarm.runtime.request import CHAT_TURN_METHODS

    assert {method.value for method in CHAT_TURN_METHODS} <= CHAT_TURN_METHOD_VALUES


@pytest.mark.parametrize(
    "method,expected",
    [
        (ReqMethod.FILES_LIST, "files.list"),
        ("files.list", "files.list"),
        (None, None),
    ],
)
def test_req_method_value_handles_enum_and_string(method: object, expected: object) -> None:
    assert req_method_value(method) == expected


def test_method_not_found_payload_matches_front_contract() -> None:
    assert method_not_found_payload(ReqMethod.FILES_LIST) == {
        "error": "unknown method: files.list",
        "code": "METHOD_NOT_FOUND",
    }


def test_method_not_found_stream_payload_is_a_terminal_chat_error() -> None:
    """流式 chunk 没有 ok 字段，下游只认 event_type。"""
    from jiuwenswarm.runtime.events import TERMINAL_ERROR_EVENT_TYPES

    payload = method_not_found_stream_payload(ReqMethod.FILES_LIST)

    assert payload == {
        "event_type": "chat.error",
        "error": "unknown method: files.list",
        "code": "METHOD_NOT_FOUND",
    }
    assert payload["event_type"] in TERMINAL_ERROR_EVENT_TYPES


def test_method_not_found_response_keeps_request_identity() -> None:
    request = AgentRequest(
        request_id="req-1",
        channel_id="web",
        req_method="future.extension",
        metadata={"trace_id": "t-1"},
    )

    response = method_not_found_response(request)

    assert response.request_id == "req-1"
    assert response.channel_id == "web"
    assert response.ok is False
    assert response.metadata == {"trace_id": "t-1"}
    assert response.payload["code"] == "METHOD_NOT_FOUND"


def _function_bodies(name: str) -> list[str]:
    tree = ast.parse(INTERFACE.read_text(encoding="utf-8"))
    return [
        ast.get_source_segment(INTERFACE.read_text(encoding="utf-8"), node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]


@pytest.mark.parametrize("name", ["_process_message", "_process_message_stream"])
def test_runtime_fallthrough_is_guarded(name: str) -> None:
    bodies = _function_bodies(name)
    assert bodies, f"{name} not found"
    body = bodies[0]

    assert body.count("is_chat_turn_method(request.req_method)") == 1
    guard_at = body.index("is_chat_turn_method(")
    # 必须排在所有无状态 handler 之后，否则它们会被误判成未实现方法。
    last_handler_at = max(body.index(f"self.{handler}") for handler in STATELESS_HANDLERS)
    assert last_handler_at < guard_at
    # 必须紧挨在 chat 执行器初始化之前：取无状态 handler 之后的第一处
    # _ensure_adapter，而不是函数里的最后一处，这样兜底被挪进执行体时会失败。
    adapter_at = body.index(
        "self._ensure_adapter(mode=self._adapter_mode_for_request(request))",
        last_handler_at,
    )
    assert guard_at < adapter_at


def test_stream_and_unary_consult_the_same_stateless_handlers() -> None:
    unary = _function_bodies("_process_message")[0]
    stream = _function_bodies("_process_message_stream")[0]

    for handler in STATELESS_HANDLERS:
        assert f"self.{handler}" in unary
        assert f"self.{handler}" in stream


def _bare_client():
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    # 不跑 __init__：未命中任何路由时，兜底必须在碰到 adapter 之前就返回。
    return object.__new__(JiuWenSwarm)


def test_unary_unhandled_method_returns_method_not_found() -> None:
    request = AgentRequest(
        request_id="guard-unary",
        channel_id="web",
        req_method=ReqMethod.FILES_LIST,
    )

    response = asyncio.run(
        _bare_client()._process_message(request, schedule_session=False)
    )

    assert response.ok is False
    assert response.payload == {
        "error": "unknown method: files.list",
        "code": "METHOD_NOT_FOUND",
    }


def test_stream_unhandled_method_yields_method_not_found() -> None:
    request = AgentRequest(
        request_id="guard-stream",
        channel_id="web",
        req_method="future.extension",
        is_stream=True,
    )

    async def _collect() -> list[object]:
        return [chunk async for chunk in _bare_client()._process_message_stream(request)]

    chunks = asyncio.run(_collect())

    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    assert chunks[0].payload == {
        "event_type": "chat.error",
        "error": "unknown method: future.extension",
        "code": "METHOD_NOT_FOUND",
    }


class _ReachedChatExecutor(Exception):
    """标记请求越过兜底、到达 chat 执行器。"""


def test_stream_user_answer_reaches_chat_executor() -> None:
    """流式入口没有 chat.user_answer 专属分支，它靠聊天链路续跑，不能被兜底拦下。"""
    client = _bare_client()
    client._adapter_mode_for_request = lambda _request: "agent"

    def _ensure_adapter(**_kwargs):
        raise _ReachedChatExecutor

    client._ensure_adapter = _ensure_adapter
    request = AgentRequest(
        request_id="guard-stream-answer",
        channel_id="http",
        req_method=ReqMethod.CHAT_ANSWER,
        is_stream=True,
    )

    async def _collect() -> list[object]:
        return [chunk async for chunk in client._process_message_stream(request)]

    with pytest.raises(_ReachedChatExecutor):
        asyncio.run(_collect())
