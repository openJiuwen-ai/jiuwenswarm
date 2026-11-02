# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P3 中断恢复体验加固测试。

覆盖三块契约：
1. 卡片实例活性登记（P3-1）：新卡注册顶替同 base 旧卡——旧卡进死卡集
   （供 guard_stale_interrupt_response 拒绝其应答），活卡指针更新；
   同 base 第二张卡发出时 request_id 改写为 ``{id}#{n}`` 序号后缀。
2. 挂起表复用与作答消费销毁（方案二）：活卡未作答时跨请求重放同
   questions 卡 → 重发原卡（不生成新代，切断乱序点击事故链）；作答被
   接受即销毁挂起条目（不进死卡集）——回答"还挂着没有"而非"发过没有"。
3. 批次级授权（P3-2，agent-core 侧）：resume 重放并行批次前，
   ``_collect_batch_allow_keys`` 收集本批已批准调用的 auto-confirm key，
   rail first_check 命中 ``permission.batch_allow.hit`` 放行同批同工具的
   未批准调用——「点一张卡，整批放行」，不再逐张赶尸弹卡。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.skill_turbo.hitl_dedup import (
    OuterCallIDDeduplicator,
)

# ────────────────── P3-1: 被取代卡失效广播 ──────────────────


def _make_adapter(**attrs) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._outer_call_dedup = OuterCallIDDeduplicator()
    adapter._hitl_card_instances = {}
    adapter._hitl_base_live_instance = {}
    adapter._hitl_base_live_questions = {}
    adapter._hitl_dead_card_ids = set()
    adapter._ask_user_card_seq = {}
    adapter._is_session_scoped_adapter = True
    for name, value in attrs.items():
        setattr(adapter, name, value)
    return adapter


def test_register_supersedes_prev_live() -> None:
    """新卡注册顶替旧卡：旧卡进死卡集，活卡指针更新。"""
    adapter = _make_adapter()
    adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-a")

    adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-b")
    assert "card-a" in adapter._hitl_dead_card_ids
    assert adapter._hitl_base_live_instance["tc-1"] == "card-b"

    # 同卡重复登记（幂等场景）：无新增死卡
    adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-b")
    assert adapter._hitl_dead_card_ids == {"card-a"}


def _ask_card_payload(request_id: str, question: str = "允许执行该工具?") -> dict:
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "source": "permission_interrupt",
        "questions": [{"question": question, "options": ["本次允许"]}],
    }


def test_dedupe_rewrites_second_card_seq() -> None:
    """同 base 第二张卡（不同 questions）发出时，request_id 改写为 {id}#{n}。"""
    adapter = _make_adapter()
    emitted_ids: set[str] = set()
    emitted_questions: dict[str, str] = {}

    first = _ask_card_payload("tc-1", "允许执行 web_search?")
    assert adapter._dedupe_ask_user_card(first, emitted_ids, emitted_questions) is False
    assert first["request_id"] == "tc-1"  # 首张卡不带序号

    second = _ask_card_payload("tc-1", "允许执行 web_search? （新参数）")
    assert adapter._dedupe_ask_user_card(second, emitted_ids, emitted_questions) is False
    assert second["request_id"] == "tc-1#2"  # 序号后缀成为新卡 ID


def test_stale_answer_rejects_non_live_generation_of_known_base() -> None:
    """同 base 顺序门 + 同权限 re-ask 迟到应答的守卫语义（CR-1/BUG20260928373903）。

    - 门 1 应答（首代卡）消费后，门 2（新一代 tc-1#2，不同权限内容）应答必须
      放行——顺序权限门不得被误判为重复（CR-1 回归锚点）。
    - 旧代卡迟到应答（死卡集命中）与未登记的更晚代次（伪造/淘汰后重放，
      base 当前代卡在案）判陈旧——同权限 re-ask 风暴的迟到重复由此拦截。
    - base 未登记 / 无后缀未登记：fail-open（重启恢复、未走登记发卡路径）。
    """
    adapter = _make_adapter()
    # 门 1：base tc-1 首代卡（无后缀）
    assert adapter._register_hitl_card_instance(base_id="tc-1", final_id="tc-1") is None
    assert adapter._is_stale_hitl_card_answer("tc-1") is False

    # 门 2：不同权限内容 → 新一代 tc-1#2，门 1 卡被顶替进死卡集
    assert adapter._register_hitl_card_instance(base_id="tc-1", final_id="tc-1#2") is None
    assert "tc-1" in adapter._hitl_dead_card_ids

    # 当前代卡放行（顺序下一个权限门的合法应答）
    assert adapter._is_stale_hitl_card_answer("tc-1#2") is False
    # 旧代卡迟到应答：死卡集命中
    assert adapter._is_stale_hitl_card_answer("tc-1") is True
    # 未登记的更晚代次（伪造代次 / 登记表淘汰后重放）：base 在案且非当前代 → 陈旧
    assert adapter._is_stale_hitl_card_answer("tc-1#9") is True
    # 未登记且 base 也未登记：fail-open
    assert adapter._is_stale_hitl_card_answer("tc-other#3") is False
    # 未登记且无后缀：fail-open
    assert adapter._is_stale_hitl_card_answer("tc-unknown") is False
    # 同代重复应答不在此判定（由 PermissionResponseLedger 按原始 id 幂等去重）


def test_stale_answer_after_round_end_invalidation() -> None:
    """轮次结束全失效后，包括当前代卡在内的所有应答均判陈旧。"""
    adapter = _make_adapter()
    adapter._register_hitl_card_instance(base_id="tc-1", final_id="tc-1#2")
    adapter._invalidate_all_hitl_card_instances()

    assert adapter._is_stale_hitl_card_answer("tc-1#2") is True


def test_mark_round_end_invalidates_live_cards() -> None:
    """轮次正常收尾：注册表残留活卡全部转死卡，同卡再答命中 stale 判定。

    收尾点语义（协议文档断言 11）：历史轮已答/被绕开的卡片在本轮流结
    束后不得再被接受作答——此时 runner 已无挂起应答，若不失效则答案被
    fail-open 静默吞掉。
    """
    adapter = _make_adapter()
    adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-a")
    adapter._register_hitl_card_instance(base_id="tc-2", final_id="card-b")
    assert adapter._is_stale_hitl_card_answer("card-a") is False  # 收尾前活卡不拒

    adapter._mark_round_end_inmemory()

    # 全部活卡转死卡，live 注册表清空
    assert adapter._hitl_dead_card_ids == {"card-a", "card-b"}
    assert adapter._hitl_base_live_instance == {}
    assert adapter._is_stale_hitl_card_answer("card-a") is True
    assert adapter._is_stale_hitl_card_answer("card-b") is True

    # 幂等：重复收尾不报错、不改变状态
    adapter._mark_round_end_inmemory()
    assert adapter._hitl_dead_card_ids == {"card-a", "card-b"}

    # 收尾后新轮次新卡照常活卡（历史失效不影响下一代）
    adapter._register_hitl_card_instance(base_id="tc-3", final_id="card-c")
    assert adapter._is_stale_hitl_card_answer("card-c") is False
    assert adapter._hitl_base_live_instance["tc-3"] == "card-c"


@pytest.mark.asyncio
async def test_guard_rejects_answer_after_round_end() -> None:
    """轮次收尾后同卡再答：活性卡守卫回 True（拒绝），不再 fail-open 吞答案。"""
    adapter = _make_adapter(_instance=None)
    adapter._register_hitl_card_instance(base_id="tc-1", final_id="card-a")
    request = AgentRequest(
        request_id="req-9",
        params={"request_id": "card-a"},
        session_id="sess-1",
    )

    # 收尾前：活卡不拒（_instance=None 走 fail-open 短路）
    assert await adapter.guard_stale_interrupt_response(request) is False

    adapter._mark_round_end_inmemory()
    # 收尾后：死卡命中守卫 → stale_interrupt_response 拒绝
    assert await adapter.guard_stale_interrupt_response(request) is True


# ────────────────── 挂起表复用与作答消费销毁（方案二） ──────────────────


def test_dedupe_reuses_live_card_across_requests() -> None:
    """挂起表复用：活卡未作答时，跨请求重放同 questions 卡 → 重发原卡。

    乱序点击事故形态的切断点：resume 重放重新走到未作答 interrupt 的发卡
    点时，不得诞生 ``{id}#{n+1}`` 新卡取代原卡——否则用户点回原卡即被
    stale 守卫拒绝，空流被 relay-claw 空回弹启发式误判中止。
    """
    adapter = _make_adapter()
    # 请求 A：首卡发出，流中断等待作答
    first = _ask_card_payload("tc-1", "允许执行 web_search?")
    assert adapter._dedupe_ask_user_card(first, set(), {}) is False
    assert adapter._ask_user_card_seq["tc-1"] == 1

    # 请求 B（乱序点击兄弟卡触发的 resume）：局部去重集合是新的，重放同一卡
    replay = _ask_card_payload("tc-1", "允许执行 web_search?")
    replay_ids: set[str] = set()
    replay_questions: dict[str, str] = {}
    assert adapter._dedupe_ask_user_card(replay, replay_ids, replay_questions) is False
    # 重发原卡：request_id 不变（未生成 #2 新代），代次计数不增长
    assert replay["request_id"] == "tc-1"
    assert adapter._ask_user_card_seq["tc-1"] == 1
    assert adapter._hitl_base_live_instance["tc-1"] == "tc-1"

    # 重放流内多通道重复（同 questions 二次出现）：由局部集合跳过
    assert (
        adapter._dedupe_ask_user_card(
            _ask_card_payload("tc-1", "允许执行 web_search?"),
            replay_ids,
            replay_questions,
        )
        is True
    )


def test_dedupe_reuses_current_generation_final_id() -> None:
    """活卡为 #n 代时，重放同 questions 卡复用该代 final_id 而非再造 #n+1。"""
    adapter = _make_adapter()
    assert adapter._dedupe_ask_user_card(_ask_card_payload("tc-1", "q1"), set(), {}) is False
    # 不同 questions → 合法 re-ask 新代 tc-1#2（既有语义保持）
    assert adapter._dedupe_ask_user_card(_ask_card_payload("tc-1", "q2"), set(), {}) is False
    assert adapter._hitl_base_live_instance["tc-1"] == "tc-1#2"

    # 重放撞见 q2（当前活卡 questions）→ 复用 #2 代原卡
    replay = _ask_card_payload("tc-1", "q2")
    assert adapter._dedupe_ask_user_card(replay, set(), {}) is False
    assert replay["request_id"] == "tc-1#2"
    assert adapter._ask_user_card_seq["tc-1"] == 2  # 未增长


def test_consume_hitl_live_card_destroys_pending_entry() -> None:
    """作答消费销毁：活条目摘除（live/questions），不进死卡集。"""
    adapter = _make_adapter()
    assert adapter._dedupe_ask_user_card(_ask_card_payload("tc-1", "q1"), set(), {}) is False
    request = AgentRequest(
        request_id="req-1",
        params={"request_id": "tc-1", "source": "ask_user_interrupt", "answers": [{"answer": "ok"}]},
        session_id="sess-1",
    )
    adapter._consume_hitl_live_card_for_answer(request)

    assert "tc-1" not in adapter._hitl_base_live_instance
    assert "tc-1" not in adapter._hitl_base_live_questions
    # 不进死卡集：同代重复应答归 PermissionResponseLedger 按原始 id 幂等去重
    assert "tc-1" not in adapter._hitl_dead_card_ids
    assert adapter._is_stale_hitl_card_answer("tc-1") is False

    # 消费后重放同 questions 卡：挂起表未命中 → 走新代兜底（tc-1#2）
    replay = _ask_card_payload("tc-1", "q1")
    assert adapter._dedupe_ask_user_card(replay, set(), {}) is False
    assert replay["request_id"] == "tc-1#2"


def test_consume_strips_seq_suffix_to_base() -> None:
    """带 {id}#{n} 后缀的作答消费：剥到 base 销毁挂起条目。"""
    adapter = _make_adapter()
    assert adapter._dedupe_ask_user_card(_ask_card_payload("tc-1", "q1"), set(), {}) is False
    assert adapter._dedupe_ask_user_card(_ask_card_payload("tc-1", "q2"), set(), {}) is False
    assert adapter._hitl_base_live_instance["tc-1"] == "tc-1#2"

    request = AgentRequest(
        request_id="req-2",
        params={"request_id": "tc-1#2"},
        session_id="sess-1",
    )
    adapter._consume_hitl_live_card_for_answer(request)
    assert "tc-1" not in adapter._hitl_base_live_instance
    assert "tc-1" not in adapter._hitl_base_live_questions


def test_invalidate_clears_pending_questions_table() -> None:
    """轮次收尾：questions 挂起表随 live 表同步清空，历史失效不复活。"""
    adapter = _make_adapter()
    assert adapter._dedupe_ask_user_card(_ask_card_payload("tc-1", "q1"), set(), {}) is False
    assert adapter._hitl_base_live_questions["tc-1"]

    adapter._invalidate_all_hitl_card_instances()
    assert adapter._hitl_base_live_instance == {}
    assert adapter._hitl_base_live_questions == {}
    # 收尾后同 questions 重发：live 未命中 → 走新代（不复活已失效卡）
    replay = _ask_card_payload("tc-1", "q1")
    assert adapter._dedupe_ask_user_card(replay, set(), {}) is False
    assert replay["request_id"] == "tc-1#2"


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


@pytest.mark.asyncio
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
