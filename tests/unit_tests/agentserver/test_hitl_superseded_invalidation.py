# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P3 中断恢复体验加固测试。

覆盖两块契约：
1. 被取代卡失效广播（P3-1）：新卡注册顶替同 base 旧卡时，注册表返回被取代
   卡 ID，``_dedupe_ask_user_card`` 在 payload 打内部标记，流式转发层用
   ``_pop_superseded_expiry_chunk`` 在新卡 chunk 之前构造
   ``chat.ask_user_question_expired(reason=superseded)`` 精确失效旧卡——
   前端屏幕上不再堆积可点击的死卡。
2. 批次级授权（P3-2，agent-core 侧）：resume 重放并行批次前，
   ``_collect_batch_allow_keys`` 收集本批已批准调用的 auto-confirm key，
   rail first_check 命中 ``permission.batch_allow.hit`` 放行同批同工具的
   未批准调用——「点一张卡，整批放行」，不再逐张赶尸弹卡。
"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

# ────────────────── P3-1: 被取代卡失效广播 ──────────────────


def _make_adapter(**attrs) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._hitl_card_instances = {}
    adapter._hitl_base_live_instance = {}
    adapter._hitl_dead_card_ids = set()
    adapter._ask_user_card_seq = {}
    adapter._is_session_scoped_adapter = True
    for name, value in attrs.items():
        setattr(adapter, name, value)
    return adapter


def test_register_returns_superseded_prev_live() -> None:
    """新卡注册顶替旧卡：返回被取代的旧卡 ID，旧卡进死卡集。"""
    adapter = _make_adapter()
    superseded = adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-a")
    assert superseded is None  # 首张卡无旧卡

    superseded = adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-b")
    assert superseded == "card-a"
    assert "card-a" in adapter._hitl_dead_card_ids
    assert adapter._hitl_base_live_instance["tc-1"] == "card-b"

    # 同卡重复登记（幂等场景）：无被取代
    superseded = adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-b")
    assert superseded is None


def _ask_card_payload(request_id: str, question: str = "允许执行该工具?") -> dict:
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "source": "permission_interrupt",
        "questions": [{"question": question, "options": ["本次允许"]}],
    }


def test_dedupe_marks_superseded_payload() -> None:
    """同 base 第二张卡（不同 questions）发出时，payload 携带被取代旧卡标记。"""
    adapter = _make_adapter()
    emitted_ids: set[str] = set()
    emitted_questions: dict[str, str] = {}

    first = _ask_card_payload("tc-1", "允许执行 web_search?")
    assert adapter._dedupe_ask_user_card(first, emitted_ids, emitted_questions) is False
    assert "_superseded_request_id" not in first  # 首张无标记

    second = _ask_card_payload("tc-1", "允许执行 web_search? （新参数）")
    assert adapter._dedupe_ask_user_card(second, emitted_ids, emitted_questions) is False
    assert second["_superseded_request_id"] == "tc-1"
    assert second["request_id"] == "tc-1#2"  # 序号后缀成为新卡 ID


def test_pop_superseded_expiry_chunk_builds_event() -> None:
    """expiry helper 构造精确失效事件，并从新卡 payload 移除内部标记。"""
    adapter = _make_adapter()
    parsed = _ask_card_payload("tc-1#2")
    parsed["_superseded_request_id"] = "tc-1"

    chunk = adapter._pop_superseded_expiry_chunk(
        parsed,
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
    )
    assert chunk is not None
    assert chunk.request_id == "req-1"
    assert chunk.channel_id == "web"
    payload = chunk.payload
    assert payload["event_type"] == "chat.ask_user_question_expired"
    assert payload["request_id"] == "tc-1"  # 失效的是旧卡
    assert payload["reason"] == "superseded"
    assert payload["session_id"] == "sess-1"
    assert payload["source"] == "permission_interrupt"
    # 内部标记已移除——新卡 chunk 发给前端时不携带内部字段
    assert "_superseded_request_id" not in parsed

    # 无标记 / 非 dict：no-op
    assert adapter._pop_superseded_expiry_chunk(parsed, request_id="req-1", channel_id="web") is None
    assert adapter._pop_superseded_expiry_chunk(None, request_id="req-1", channel_id="web") is None


def test_pop_superseded_expiry_omits_blank_fields() -> None:
    """session_id / source 缺失时事件不带空字段（前端按需解析）。"""
    adapter = _make_adapter()
    parsed = {"_superseded_request_id": "old-1"}
    chunk = adapter._pop_superseded_expiry_chunk(
        parsed, request_id="req-1", channel_id="web"
    )
    assert chunk is not None
    assert "session_id" not in chunk.payload
    assert "source" not in chunk.payload


# ────────────────── P3-2: 批次级授权（agent-core 侧） ──────────────────


def _batch_state():
    """两调用批次：web_search（已批） + web_search（未批）。"""
    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.single_agent.interrupt.state import (
        ToolInterruptEntry,
        ToolInterruptionState,
    )

    tc1 = ToolCall(id="c-1", type="function", name="web_search", arguments='{"query": "a"}')
    tc2 = ToolCall(id="c-2", type="function", name="web_search", arguments='{"query": "b"}')
    state = ToolInterruptionState(
        ai_message=AssistantMessage(content=""),
        iteration=0,
        interrupted_tools={
            "c-1": ToolInterruptEntry(tool_call=tc1),
            "c-2": ToolInterruptEntry(tool_call=tc2),
        },
    )
    return state, tc1, tc2


def test_collect_batch_allow_keys_from_approved_sibling() -> None:
    """同批已批准（allow_once）的调用 → 收集其参数指纹 key（tool:args-hash），
    仅同工具同参数的兄弟调用可共享放行（P3-2 收紧后不再扩散裸工具名）。"""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from openjiuwen.core.single_agent.interrupt.handler import ToolInterruptHandler
    from openjiuwen.harness.rails.security.tool_security_rail import (
        compute_batch_allow_key,
    )

    state, _tc1, _tc2 = _batch_state()
    user_input = InteractiveInput()
    user_input.update("c-1", {"approved": True, "auto_confirm": False})

    keys = ToolInterruptHandler._collect_batch_allow_keys(state, user_input)
    expected_key = compute_batch_allow_key(_tc1)
    assert keys == {expected_key}
    assert expected_key.startswith("web_search:")  # 参数指纹 key，非裸工具名


def test_collect_batch_allow_keys_skips_rejected_and_missing() -> None:
    """被拒绝 / 未应答的调用不贡献 key。"""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from openjiuwen.core.single_agent.interrupt.handler import ToolInterruptHandler

    state, _tc1, _tc2 = _batch_state()
    rejected = InteractiveInput()
    rejected.update("c-1", {"approved": False})
    assert ToolInterruptHandler._collect_batch_allow_keys(state, rejected) == set()

    empty = InteractiveInput()
    assert ToolInterruptHandler._collect_batch_allow_keys(state, empty) == set()

    # 非 InteractiveInput 载荷 fail-open
    assert ToolInterruptHandler._collect_batch_allow_keys(state, {"c-1": {"approved": True}}) == set()


def test_compute_auto_confirm_key_semantics() -> None:
    """key 语义与 rail 会话级 auto_confirm 一致：工具名 / shell 子命令。"""
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.harness.rails.security.tool_security_rail import (
        PermissionInterruptRail,
        compute_auto_confirm_key,
    )

    tc = ToolCall(id="c", type="function", name="web_search", arguments='{"query": "x"}')
    assert compute_auto_confirm_key(tc) == "web_search"

    shell = ToolCall(id="c", type="function", name="bash", arguments='{"command": "git status"}')
    assert compute_auto_confirm_key(shell) == "bash:git status"

    # 与 rail 实例方法口径一致（批内 key 计算不得漂移）
    rail = object.__new__(PermissionInterruptRail)
    assert rail._get_auto_confirm_key(tc) == compute_auto_confirm_key(tc)
    assert rail._get_auto_confirm_key(shell) == compute_auto_confirm_key(shell)


def test_resume_batch_allow_keys_constant_matches_rail() -> None:
    """handler 注入的 extra key 与 rail 读取的常量是同一符号。"""
    from openjiuwen.core.single_agent.interrupt.state import RESUME_BATCH_ALLOW_KEYS

    assert RESUME_BATCH_ALLOW_KEYS == "_resume_batch_allow_keys"
    # rail 模块已 import 该常量（批内检查点使用）
    from openjiuwen.harness.rails.security import tool_security_rail

    assert tool_security_rail.RESUME_BATCH_ALLOW_KEYS is RESUME_BATCH_ALLOW_KEYS


async def test_handle_resume_injects_and_clears_batch_allow_keys(monkeypatch) -> None:
    """handle_resume：重放前注入批内 allow 集，finally 清理（不泄漏到后续批次）。"""
    from openjiuwen.core.single_agent.interrupt.handler import ToolInterruptHandler
    from openjiuwen.core.single_agent.interrupt.state import RESUME_BATCH_ALLOW_KEYS
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
    from openjiuwen.harness.rails.security.tool_security_rail import (
        compute_batch_allow_key,
    )

    state, _tc1, _tc2 = _batch_state()
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    user_input = InteractiveInput()
    user_input.update("c-1", {"approved": True, "auto_confirm": False})

    ctx = AgentCallbackContext(agent=None, session=None, inputs=None, context=None, extra={})
    captured: dict = {}

    async def fake_execute_tool_call(ctx_arg, tools, session, context):
        captured["batch_allow"] = ctx_arg.extra.get(RESUME_BATCH_ALLOW_KEYS)
        return []

    resume_ctx = SimpleNamespace(
        state=state,
        user_input=user_input,
        ctx=ctx,
        context=None,
        session=None,
        invoke_inputs=None,
        execute_tool_call=fake_execute_tool_call,
    )

    handler = ToolInterruptHandler.__new__(ToolInterruptHandler)
    result = await handler.handle_resume(resume_ctx)

    assert result is None  # 无新中断 → 继续 ReAct 循环
    # 批内 allow 集为已批调用的参数指纹 key（P3-2：同工具同参数才共享放行）
    assert captured["batch_allow"] == {compute_batch_allow_key(_tc1)}
    # finally 清理：extra 不残留（下一轮新批次重新弹卡询问）
    assert RESUME_BATCH_ALLOW_KEYS not in ctx.extra
