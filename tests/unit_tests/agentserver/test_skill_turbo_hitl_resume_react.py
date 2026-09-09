# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HITL resume should re-invoke skill_acceleration_exec so ReAct can summarize."""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
    SKILL_TURBO_RESUME_CTX_KEY,
    load_resume_ctx,
    mark_resume_in_flight,
    save_resume_ctx,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    _SKILL_TURBO_HITL_PLACEHOLDER,
    _SKILL_TURBO_STOP_HINT_NEUTRAL,
    _resolve_skill_turbo_resume_session_id,
    _resume_user_input_from_raw,
    get_skill_turbo_resume_answers,
    reset_skill_turbo_resume_answers,
    set_skill_turbo_hitl_tic,
    set_skill_turbo_resume_answers,
)

# 旧版停止提示文案（宣称文件已发送），生产代码已删除常量；保留字符串仅用于
# 验证存量会话历史（tool_result 嵌旧文案）重放时被 _fix_incomplete_tool_context
# 原样保留——该修复逻辑按 tool_call_id 结构匹配，不依赖常量存在。
_LEGACY_STOP_HINT_TEXT = (
    "\n\n[SYSTEM] The skill_acceleration_exec task is complete and the artifact has already been "
    "generated. The file(s) have ALREADY been sent to the user by the internal "
    "delivery pipeline - do NOT call send_file_to_user again. You should now "
    "summarize this result to the user and finish your turn. Do NOT call "
    "skill_acceleration_exec, skill_tool, or send_file_to_user again for this task - the "
    "work is already done; calling any of them again would duplicate the work."
)


class _StreamSession:
    def __init__(self):
        self.chunks = []

    async def write_stream(self, chunk):
        self.chunks.append(chunk)


def _tool_ctx(session, tool_name: str):
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    tool_call = SimpleNamespace(id="call-1", name=tool_name, arguments={})
    return SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args={},
            tool_result={"success": True},
        ),
        extra={},
        exception=None,
    )


def test_deep_agent_has_skill_turbo_interrupt():
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = None
    assert adapter._deep_agent_has_skill_turbo_interrupt() is False

    adapter._instance = SimpleNamespace(
        _loop_session=SimpleNamespace(
            get_state=lambda _key: SimpleNamespace(
                interrupted_tools={
                    "call_1": SimpleNamespace(
                        tool_call=SimpleNamespace(name="skill_acceleration_exec")
                    )
                }
            )
        )
    )
    assert adapter._deep_agent_has_skill_turbo_interrupt() is True

    adapter._instance._loop_session.get_state = lambda _key: SimpleNamespace(
        interrupted_tools={
            "call_1": SimpleNamespace(tool_call=SimpleNamespace(name="ask_user"))
        }
    )
    assert adapter._deep_agent_has_skill_turbo_interrupt() is False


@pytest.mark.asyncio
async def test_try_skill_turbo_resume_defers_when_outer_interrupt_and_no_ctx(monkeypatch):
    """Outer interrupt alone still defers when there is no SkillTurbo resume_ctx."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(
        card=object(),
        _loop_session=SimpleNamespace(
            get_state=lambda _key: SimpleNamespace(
                interrupted_tools={
                    "call_1": SimpleNamespace(
                        tool_call=SimpleNamespace(name="skill_acceleration_exec")
                    )
                }
            )
        ),
    )
    request = AgentRequest(
        request_id="req-1",
        channel_id="officeclaw",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "answers": [{"question": "页数", "selected_options": ["10"]}],
            "source": "ask_user_interrupt",
            "request_id": "skill_turbo-tc-ask_user-1",
        },
    )

    class _Session:
        async def post_run(self):
            return None

        def update_state(self, _state):
            return None

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _Session(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_set_agent_id",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_load_resume_ctx",
        lambda _session: _async_none(),
    )
    assert await adapter._try_skill_turbo_resume(request, {}) is None


async def _async_none():
    return None


async def _async_resume_ctx():
    return {"pending_tool_call_id": "skill_turbo-tc-ask_user-1", "plan_code": "x"}


@pytest.mark.asyncio
async def test_try_skill_turbo_resume_continues_with_outer_interrupt_when_ctx(
    monkeypatch,
):
    """Nested ask_user: resume_ctx present must not defer to DeepAgent."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(
        card=object(),
        _loop_session=SimpleNamespace(
            get_state=lambda _key: SimpleNamespace(
                interrupted_tools={
                    "call_1": SimpleNamespace(
                        tool_call=SimpleNamespace(name="skill_acceleration_exec")
                    )
                }
            )
        ),
    )
    sentinel = object()
    adapter._make_skill_turbo_resume_stream = (
        lambda **_kwargs: sentinel  # type: ignore[method-assign]
    )
    request = AgentRequest(
        request_id="req-1",
        channel_id="officeclaw",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "answers": [{"question": "页数", "selected_options": ["10"]}],
            "source": "ask_user_interrupt",
            "request_id": "skill_turbo-tc-ask_user-1",
        },
    )

    class _Session:
        async def post_run(self):
            return None

        def update_state(self, _state):
            return None

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _Session(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_set_agent_id",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_load_resume_ctx",
        lambda _session: _async_resume_ctx(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_mark_resume_in_flight",
        lambda _session, _ctx: _async_none(),
    )
    assert await adapter._try_skill_turbo_resume(request, {}) is sentinel


async def _async_in_flight_resume_ctx():
    return {
        "pending_tool_call_id": "skill_turbo-tc-ask_user-1",
        "plan_code": "x",
        "resume_in_flight": True,
    }


@pytest.mark.asyncio
async def test_try_skill_turbo_resume_ignores_duplicate_while_in_flight(monkeypatch):
    """Second answer submit while resume is running must not start another stream."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(card=object(), _loop_session=None)
    called = {"resume_stream": False}

    def _should_not_run(**_kwargs):
        called["resume_stream"] = True
        raise AssertionError("duplicate resume must not start resume_stream")

    adapter._make_skill_turbo_resume_stream = _should_not_run  # type: ignore[method-assign]
    request = AgentRequest(
        request_id="req-dup",
        channel_id="officeclaw",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "answers": [{"question": "页数", "selected_options": ["10"]}],
            "source": "ask_user_interrupt",
            "request_id": "skill_turbo-tc-ask_user-1",
        },
    )

    class _Session:
        async def post_run(self):
            return None

        def update_state(self, _state):
            return None

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _Session(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_set_agent_id",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_load_resume_ctx",
        lambda _session: _async_in_flight_resume_ctx(),
    )
    stream = await adapter._try_skill_turbo_resume(request, {})
    assert stream is not None
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    assert chunks[0].payload is None
    assert called["resume_stream"] is False


@pytest.mark.asyncio
async def test_mark_resume_in_flight_persists_across_sessions():
    """in-flight flag must survive post_run so a second session load sees it."""
    checkpoint: dict = {}

    class _CheckpointSession:
        def __init__(self):
            self._state: dict = {}

        async def pre_run(self, inputs=None):
            self._state = copy.deepcopy(checkpoint)

        async def post_run(self):
            checkpoint.clear()
            checkpoint.update(copy.deepcopy(self._state))

        def update_state(self, mapping):
            self._state.update(mapping)

        def get_state(self, key):
            return self._state.get(key)

    ctx = {
        "plan_code": "plan-x",
        "pending_tool_call_id": "skill_turbo-tc-ask_user-1",
        "inputs": {},
    }
    writer = _CheckpointSession()
    await writer.pre_run()
    writer.update_state({SKILL_TURBO_RESUME_CTX_KEY: ctx})
    await writer.post_run()

    marker = _CheckpointSession()
    loaded = await load_resume_ctx(marker)
    assert loaded is not None
    assert loaded.get("resume_in_flight") is not True
    await mark_resume_in_flight(marker, loaded)

    reader = _CheckpointSession()
    again = await load_resume_ctx(reader)
    assert again is not None
    assert again.get("resume_in_flight") is True
    assert again.get("pending_tool_call_id") == "skill_turbo-tc-ask_user-1"


@pytest.mark.asyncio
async def test_save_resume_ctx_clears_stale_resume_in_flight():
    """嵌套 ask_user 中断后 save_resume_ctx 必须清除上一轮 mark_resume_in_flight 残留标志。

    场景：第一次 ask_user 中断→恢复时 mark_resume_in_flight 设置 resume_in_flight=True；
    恢复过程中第二次 ask_user 中断→save_resume_ctx 保存新 resume_ctx。
    openjiuwen session.update_state 使用 merge 语义（update_dict），新 entry 缺少
    resume_in_flight 键不会触发删除。修复后 save_resume_ctx 显式置 None 让 merge
    将其从持久化状态中移除，下一轮 load_resume_ctx 看不到残留标志。
    """
    from openjiuwen.core.session.utils import update_dict

    checkpoint: dict = {}

    class _MergeCheckpointSession:
        """模拟 openjiuwen session + checkpointer 的 merge 语义。

        update_state 走 update_dict（与真实 InMemoryStateLike.update 一致）：
        值为 None 的键会被删除，而不是保留。
        """

        def __init__(self):
            self._state: dict = {}

        async def pre_run(self, inputs=None):
            self._state = copy.deepcopy(checkpoint)

        async def post_run(self):
            checkpoint.clear()
            checkpoint.update(copy.deepcopy(self._state))

        def update_state(self, mapping):
            update_dict(mapping, self._state)

        def get_state(self, key):
            return copy.deepcopy(self._state.get(key))

    # 第一次中断：保存 resume_ctx（无 resume_in_flight）
    writer1 = _MergeCheckpointSession()
    await writer1.pre_run()
    await save_resume_ctx(
        writer1,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-1",
        task_states=None,
    )
    await writer1.post_run()

    # 第一次恢复：mark_resume_in_flight 设置 resume_in_flight=True
    marker = _MergeCheckpointSession()
    loaded = await load_resume_ctx(marker)
    assert loaded is not None
    assert loaded.get("resume_in_flight") is not True
    await mark_resume_in_flight(marker, loaded)

    # 第二次中断：save_resume_ctx 保存新 resume_ctx（新 tcid）
    writer2 = _MergeCheckpointSession()
    await save_resume_ctx(
        writer2,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-2",
        task_states=None,
    )
    await writer2.post_run()

    # 下一轮恢复加载：resume_in_flight 必须为 False
    reader = _MergeCheckpointSession()
    again = await load_resume_ctx(reader)
    assert again is not None
    assert again.get("pending_tool_call_id") == "skill_turbo-tc-ask_user-2"
    assert not again.get("resume_in_flight"), (
        "save_resume_ctx 必须清除上一轮 mark_resume_in_flight 残留的 resume_in_flight 标志"
    )


@pytest.mark.asyncio
async def test_emit_skill_turbo_hitl_keeps_pending_tool_call_request_id(monkeypatch):
    """HITL card request_id must stay skill_turbo-tc-* (not HTTP request id)."""
    pending_tcid = "skill_turbo-tc-ask_user-9"
    http_rid = "http-req-abc"
    tool_call = SimpleNamespace(
        id=pending_tcid,
        name="ask_user",
        arguments={"questions": [{"question": "页数"}]},
    )
    tic = SimpleNamespace(tool_call=tool_call, request=SimpleNamespace())

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_extract_tool_interrupt",
        lambda _exc: tic,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_build_interaction_output",
        lambda _exc: SimpleNamespace(payload={"id": pending_tcid}),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.convert_interactions_to_ask_user_question",
        lambda _items: {
            "event_type": "chat.ask_user_question",
            "request_id": pending_tcid,
            "questions": [{"question": "页数"}],
            "source": "ask_user_interrupt",
        },
    )

    request = AgentRequest(
        request_id=http_rid,
        channel_id="officeclaw",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )
    chunks = [
        chunk
        async for chunk in JiuWenSwarmDeepAdapter._emit_skill_turbo_hitl_chunks(
            request, RuntimeError("abort")
        )
    ]
    ask = next(
        c
        for c in chunks
        if isinstance(c.payload, dict)
        and c.payload.get("event_type") == "chat.ask_user_question"
    )
    assert ask.payload["request_id"] == pending_tcid
    assert ask.request_id == http_rid


@pytest.mark.asyncio
async def test_emit_skill_turbo_hitl_overwrites_request_id_with_outer_call_id(monkeypatch):
    """resume 内二次中断的 ask_user 卡片 request_id 对齐外层 call id。

    首个 ask_user 经 StreamEventRail TIE 重写后用外层 skill_acceleration_exec
    call id 作卡片 request_id；resume 内的二次中断走 _emit_skill_turbo_hitl_chunks，
    必须传入并使用同一 outer_call_id，否则前端/relay-claw 按 request_id 路由
    作答时找不到第二张卡 → 答案丢失 → 会话卡死。
    """
    pending_tcid = "skill_turbo-tc-ask_user-9"
    outer_call_id = "call_7780745b931647ae956a2125"
    http_rid = "http-req-abc"
    tool_call = SimpleNamespace(
        id=pending_tcid,
        name="ask_user",
        arguments={"questions": [{"question": "风格"}]},
    )
    tic = SimpleNamespace(tool_call=tool_call, request=SimpleNamespace())

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_extract_tool_interrupt",
        lambda _exc: tic,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_build_interaction_output",
        lambda _exc: SimpleNamespace(payload={"id": pending_tcid}),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.convert_interactions_to_ask_user_question",
        lambda _items: {
            "event_type": "chat.ask_user_question",
            "request_id": pending_tcid,
            "questions": [{"question": "风格"}],
            "source": "ask_user_interrupt",
        },
    )

    request = AgentRequest(
        request_id=http_rid,
        channel_id="officeclaw",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )
    chunks = [
        chunk
        async for chunk in JiuWenSwarmDeepAdapter._emit_skill_turbo_hitl_chunks(
            request, RuntimeError("abort"), outer_call_id
        )
    ]
    ask = next(
        c
        for c in chunks
        if isinstance(c.payload, dict)
        and c.payload.get("event_type") == "chat.ask_user_question"
    )
    # 卡片 request_id 被外层 call id 覆盖，与首个 ask_user 一致
    assert ask.payload["request_id"] == outer_call_id
    assert ask.request_id == http_rid


@pytest.mark.asyncio
async def test_resume_impl_second_ask_user_gets_suffix_when_outer_id_has_no_suffix(monkeypatch):
    """无后缀 params.request_id 进入 _resume_impl 时 outer_call_id 不得为空。

    复现检视问题 1：被作答卡片是"该外层 call id 的首张卡"（request_id 无
    {id}#{n} 后缀）时，派生 outer_call_id 若回退空串则修复不生效。本测试
    模拟该链路：第一张 ask_user 卡先消耗 seq，第二张卡经 _emit_skill_turbo_hitl_chunks
    + _dedupe_ask_user_card 应得到 call_x#2。
    """
    outer_call_id = "call_7780745b931647ae956a2125"  # 无 #N 后缀（首张卡被作答）
    pending_tcid = "skill_turbo-tc-ask_user-18cefd55-0"
    http_rid = "http-resume-2"
    tool_call = SimpleNamespace(
        id=pending_tcid,
        name="ask_user",
        arguments={"questions": [{"question": "风格"}]},
    )
    tic = SimpleNamespace(tool_call=tool_call, request=SimpleNamespace())

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_extract_tool_interrupt",
        lambda _exc: tic,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_build_interaction_output",
        lambda _exc: SimpleNamespace(payload={"id": pending_tcid}),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.convert_interactions_to_ask_user_question",
        lambda _items: {
            "event_type": "chat.ask_user_question",
            "request_id": pending_tcid,
            "questions": [{"question": "风格"}],
            "source": "ask_user_interrupt",
        },
    )

    request = AgentRequest(
        request_id=http_rid,
        channel_id="officeclaw",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"request_id": outer_call_id, "source": "ask_user_interrupt",
                 "answers": [{"question": "受众", "selected_options": ["企业高管"]}]},
    )

    # 测实际的派生函数（_resume_impl 调的同一静态方法），不复制逻辑
    derived = JiuWenSwarmDeepAdapter._derive_outer_call_id(
        str(request.params.get("request_id") or "")
    )
    # 关键断言：无后缀时 outer_call_id 不得为空（问题 1 修复前为空串）
    assert derived == outer_call_id, (
        "无后缀 request_id 派生 outer_call_id 为空 → 第二张卡修复不生效"
    )

    # 模拟 _resume_impl 的去重链路：首张 ask_user 卡先消耗 seq 0→1（无后缀），
    # 第二张卡经 _emit_skill_turbo_hitl_chunks 覆盖 outer_call_id 后走 _dedupe_ask_user_card
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._ask_user_card_seq = {}
    emitted_ids: set[str] = set()
    emitted_questions: dict[str, str] = {}

    # 第一张卡（首张 ask_user，seq 0→1，无后缀）
    first_card = {"event_type": "chat.ask_user_question",
                  "request_id": outer_call_id,
                  "questions": [{"question": "受众"}], "source": "ask_user_interrupt"}
    assert adapter._dedupe_ask_user_card(first_card, emitted_ids, emitted_questions) is False
    assert first_card["request_id"] == outer_call_id

    # 第二张卡：经 _emit_skill_turbo_hitl_chunks 覆盖 outer_call_id 后走 _dedupe_ask_user_card
    second_card = {"event_type": "chat.ask_user_question",
                   "request_id": derived,  # _emit_skill_turbo_hitl_chunks 用 outer_call_id 覆盖
                   "questions": [{"question": "风格"}], "source": "ask_user_interrupt"}
    assert adapter._dedupe_ask_user_card(second_card, emitted_ids, emitted_questions) is False
    # 第二张卡加 #2 后缀（首张已消耗 seq=1，第二张 seq=1→2）
    assert second_card["request_id"] == f"{outer_call_id}#2"


def test_resume_user_input_from_interactive_input():
    interactive = InteractiveInput()
    payload = {
        "status": "answered",
        "answers": [
            {"question": "受众", "selected_options": ["企业高管"]},
            {"question": "目的", "selected_options": ["工作汇报"]},
        ],
    }
    interactive.update("call_outer", payload)
    got = _resume_user_input_from_raw(interactive, {}, None)
    assert got is payload
    assert len(got["answers"]) == 2


def test_resume_user_input_from_raw_answers_list():
    answers = [{"question": "页数", "selected_options": ["10"]}]
    adapter = SimpleNamespace(
        _skill_turbo_answers_to_confirm_payload=lambda raw, _ctx: raw
    )
    assert _resume_user_input_from_raw(answers, {}, adapter) == answers


def test_resume_answers_contextvar_roundtrip():
    token = set_skill_turbo_resume_answers(["a"])
    try:
        assert get_skill_turbo_resume_answers() == ["a"]
    finally:
        reset_skill_turbo_resume_answers(token)
    assert get_skill_turbo_resume_answers() is None


def test_resolve_skill_turbo_resume_session_id_prefers_metadata():
    parent = SimpleNamespace(get_session_id=lambda: "parent-sid")
    assert _resolve_skill_turbo_resume_session_id("meta-sid", parent) == "meta-sid"


def test_resolve_skill_turbo_resume_session_id_falls_back_to_parent():
    parent = SimpleNamespace(get_session_id=lambda: "parent-sid")
    assert _resolve_skill_turbo_resume_session_id("", parent) == "parent-sid"
    assert _resolve_skill_turbo_resume_session_id(None, parent) == "parent-sid"
    assert _resolve_skill_turbo_resume_session_id("", None) == ""


@pytest.mark.asyncio
async def test_skill_acceleration_exec_resume_skips_duplicate_tool_call_emit():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _tool_ctx(session, "skill_acceleration_exec")
    ctx.extra[RESUME_USER_INPUT_KEY] = [{"question": "q", "selected_options": ["a"]}]
    await rail.before_tool_call(ctx)
    assert not any(getattr(chunk, "type", None) == "tool_call" for chunk in session.chunks)
    assert not any(getattr(chunk, "type", None) == "tool_update" for chunk in session.chunks)
    await rail.after_tool_call(ctx)


@pytest.mark.asyncio
async def test_skill_acceleration_exec_first_invoke_still_emits_tool_call():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _tool_ctx(session, "skill_acceleration_exec")
    await rail.before_tool_call(ctx)
    assert any(getattr(chunk, "type", None) == "tool_call" for chunk in session.chunks)
    await rail.after_tool_call(ctx)


def _ask_user_request(source: str) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="officeclaw",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "answers": [{"selected_options": ["本次允许"]}],
            "source": source,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["permission_interrupt", "confirm_interrupt", ""])
async def test_try_skill_turbo_resume_ignores_non_ask_user_answers(source):
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(card=object(), _loop_session=None)
    assert await adapter._try_skill_turbo_resume(_ask_user_request(source), {}) is None


def test_hitl_placeholder_is_recognized_by_context_repair():
    rail = JiuSwarmStreamEventRail()
    tool_call_id = "call_982c"
    placeholders = {
        tool_call_id: rail._tool_interrupted_message("skill_acceleration_exec"),
    }
    names = {tool_call_id: "skill_acceleration_exec"}
    leaked = ToolMessage(
        content="{'success': False, 'error': '任务已暂停等待审批'}",
        tool_call_id=tool_call_id,
    )
    placeholder = ToolMessage(
        content=_SKILL_TURBO_HITL_PLACEHOLDER,
        tool_call_id=tool_call_id,
    )
    assert rail._is_tool_interrupt_placeholder(leaked, placeholders, names) is False
    assert rail._is_tool_interrupt_placeholder(placeholder, placeholders, names) is True


@pytest.mark.asyncio
async def test_skill_turbo_hitl_after_tool_call_writes_placeholder_tool_msg():
    rail = JiuSwarmStreamEventRail()
    rail.set_skill_turbo_adapter(object())
    session = _StreamSession()
    tool_call = SimpleNamespace(
        id="call_982c",
        name="skill_acceleration_exec",
        arguments={"query": "生成PPT"},
    )
    leaked = ToolMessage(
        content="{'success': False, 'error': '任务已暂停等待审批'}",
        tool_call_id="call_982c",
    )
    ctx = SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_acceleration_exec",
            tool_args={"query": "生成PPT"},
            tool_result={"success": False, "error": "任务已暂停等待审批"},
            tool_msg=leaked,
        ),
        extra={},
        exception=None,
        request_force_finish=lambda *_args, **_kwargs: None,
    )
    inner_tc = SimpleNamespace(
        id="skill_turbo-tc-ask_user-1",
        name="ask_user",
        arguments={"questions": [{"question": "风格", "options": [{"label": "科技极简"}]}]},
    )
    tic = SimpleNamespace(
        request=SimpleNamespace(message="ask", tool_call_id="skill_turbo-tc-ask_user-1"),
        tool_call=inner_tc,
    )
    set_skill_turbo_hitl_tic(tic)
    try:
        await rail.after_tool_call(ctx)
    finally:
        set_skill_turbo_hitl_tic(None)

    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.content == rail._tool_interrupted_message(
        "skill_acceleration_exec"
    )
    assert ctx.inputs.tool_msg.tool_call_id == "call_982c"
    assert ctx.inputs.tool_msg is not leaked


class _ModelContext:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self):
        return list(self.messages)

    def pop_messages(self, size):
        popped = self.messages[:size]
        self.messages = self.messages[size:]
        return popped

    async def add_messages(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_fix_incomplete_tool_context_keeps_stop_hint_over_hitl_placeholder():
    rail = JiuSwarmStreamEventRail()
    tool_call_id = "call_982c"
    stop_hint_msg = ToolMessage(
        content="任务已完成" + _LEGACY_STOP_HINT_TEXT,
        tool_call_id=tool_call_id,
    )
    ctx = SimpleNamespace(
        context=_ModelContext([
            UserMessage(content="生成一页PPT"),
            AssistantMessage(
                content="",
                tool_calls=[{
                    "type": "function",
                    "id": tool_call_id,
                    "function": {
                        "name": "skill_acceleration_exec",
                        "arguments": "{\"query\":\"生成PPT\"}",
                    },
                }],
            ),
            ToolMessage(
                content=_SKILL_TURBO_HITL_PLACEHOLDER,
                tool_call_id=tool_call_id,
            ),
            ToolMessage(
                content=_SKILL_TURBO_HITL_PLACEHOLDER,
                tool_call_id=tool_call_id,
            ),
            stop_hint_msg,
        ]),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )

    await rail._fix_incomplete_tool_context(ctx)

    messages = ctx.context.get_messages()
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == tool_call_id
    assert "任务已暂停等待审批" not in tool_msgs[0].content
    assert _SKILL_TURBO_HITL_PLACEHOLDER not in tool_msgs[0].content
    assert "The skill_acceleration_exec task is complete" in tool_msgs[0].content
    assert "skill_tool" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_fix_incomplete_tool_context_keeps_neutral_stop_hint_over_hitl_placeholder():
    # 新版中立收尾与旧版停止提示走同一保留路径：
    # 中断 placeholder 被真实 tool_result（含中立收尾）原位替换。
    rail = JiuSwarmStreamEventRail()
    tool_call_id = "call_982d"
    neutral_msg = ToolMessage(
        content="任务已完成" + _SKILL_TURBO_STOP_HINT_NEUTRAL,
        tool_call_id=tool_call_id,
    )
    ctx = SimpleNamespace(
        context=_ModelContext([
            UserMessage(content="生成一页PPT"),
            AssistantMessage(
                content="",
                tool_calls=[{
                    "type": "function",
                    "id": tool_call_id,
                    "function": {
                        "name": "skill_acceleration_exec",
                        "arguments": "{\"query\":\"生成PPT\"}",
                    },
                }],
            ),
            ToolMessage(
                content=_SKILL_TURBO_HITL_PLACEHOLDER,
                tool_call_id=tool_call_id,
            ),
            neutral_msg,
        ]),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )

    await rail._fix_incomplete_tool_context(ctx)

    messages = ctx.context.get_messages()
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == tool_call_id
    assert _SKILL_TURBO_HITL_PLACEHOLDER not in tool_msgs[0].content
    assert "did NOT confirm" in tool_msgs[0].content
    assert "send_file_to_user" in tool_msgs[0].content
