# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P2 HITL suppress 协议化测试。

覆盖三块契约：
1. 恢复信号（``__resume_signal__``，agent-core react_agent resume 分支发出）：
   精确清除 HITL suppress 且自身不转发前端；旧版 agent-core 无信号时仍由
   "首个非噪声 chunk" 兜底清除。
2. 终态守卫（guard_stale_interrupt_response）：死卡应答（同 base 已被新卡
   取代 / 终态 invalidate）直接拒绝，活卡 / 未登记卡片 fail-open。
3. ask_user 应答识别（_is_ask_user_interrupt_response）：ask_user continuation
   不进 permission ledger，但仍是中断应答，守卫入口据此分流。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import (
    _is_ask_user_interrupt_response,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interrupt_state_machine import (
    INTERRUPT_PHASE_SESSION_KEY,
    PHASE_IDLE,
    PHASE_RESUMED,
)


class _Chunk:
    """最小 OutputSchema 形状（type + payload）。"""

    def __init__(self, chunk_type: str, payload: object = None) -> None:
        self.type = chunk_type
        self.payload = payload


# ────────────────── 恢复信号：suppress 清除决策 ──────────────────


def test_resume_signal_clears_suppress_and_skips_chunk() -> None:
    """协议化信号到达：清除 suppress 且信号本身不转发（skip=True）。"""
    chunk = _Chunk("__resume_signal__", {"source": "tool_interrupt"})
    suppress_after, skip = JiuWenSwarmDeepAdapter._apply_hitl_suppress_clear(
        chunk,
        suppress_stream_after_hitl=True,
        request_id="req-signal",
    )
    assert suppress_after is False
    assert skip is True


def test_resume_signal_is_dropped_by_parser() -> None:
    """信号无用户可见内容：_parse_stream_chunk 必须吞掉（返回 None）。"""
    chunk = _Chunk("__resume_signal__", {"source": "workflow_interrupt"})
    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None


def test_resume_signal_when_suppress_already_cleared_is_inert() -> None:
    """suppress 已清除时信号无害：不清除、不跳过，由 parser 吞掉。"""
    chunk = _Chunk("__resume_signal__", {"source": "tool_interrupt"})
    suppress_after, skip = JiuWenSwarmDeepAdapter._apply_hitl_suppress_clear(
        chunk,
        suppress_stream_after_hitl=False,
        request_id="req-signal",
    )
    assert suppress_after is False
    assert skip is False


def test_noise_chunks_keep_suppress() -> None:
    """噪声（llm_usage / context.usage / 无失败 controller_output）不清除。"""
    for payload in ("llm_usage", "context.usage"):
        suppress_after, skip = JiuWenSwarmDeepAdapter._apply_hitl_suppress_clear(
            _Chunk(payload),
            suppress_stream_after_hitl=True,
        )
        assert suppress_after is True
        assert skip is True


def test_unknown_chunk_still_clears_suppress_as_fallback() -> None:
    """旧版 agent-core 无信号：首个非噪声 chunk 兜底清除（fail-open 防挂起）。"""
    suppress_after, skip = JiuWenSwarmDeepAdapter._apply_hitl_suppress_clear(
        _Chunk("chat.delta", {"content": "hello"}),
        suppress_stream_after_hitl=True,
    )
    assert suppress_after is False
    assert skip is False


# ────────────────── 终态守卫：死卡应答拒绝 ──────────────────


def _guard_adapter(
    *,
    card_instances: dict[str, str] | None = None,
    live_instances: dict[str, str] | None = None,
    dead_ids: set[str] | None = None,
) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._hitl_card_instances = card_instances or {}
    adapter._hitl_base_live_instance = live_instances or {}
    adapter._hitl_dead_card_ids = dead_ids or set()
    # _instance=None：相位检查早退返回 False，测试聚焦死卡判定
    adapter._instance = None
    return adapter


def _ask_user_answer_request(card_id: str) -> AgentRequest:
    return AgentRequest(
        request_id="req-answer",
        channel_id="officeclaw",
        session_id="sess-guard",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "source": "ask_user_interrupt",
            "request_id": card_id,
            "answers": [{"id": card_id, "answer": "yes"}],
        },
    )


@pytest.mark.asyncio
async def test_guard_rejects_answer_to_dead_card_instance() -> None:
    """同 base 旧卡已被新卡取代：旧卡应答必须拒绝（活性卡守卫）。"""
    adapter = _guard_adapter(
        card_instances={"tcid#1": "tcid", "tcid#2": "tcid"},
        live_instances={"tcid": "tcid#2"},
    )
    assert await adapter.guard_stale_interrupt_response(
        _ask_user_answer_request("tcid#1")
    ) is True


@pytest.mark.asyncio
async def test_guard_rejects_answer_to_invalidated_card() -> None:
    """终态 invalidate（cancel/supplement）后的活卡全部转死卡：应答拒绝。"""
    adapter = _guard_adapter(
        card_instances={"tcid#1": "tcid"},
        live_instances={"tcid": "tcid#1"},
        dead_ids=set(),
    )
    adapter._invalidate_all_hitl_card_instances()
    assert await adapter.guard_stale_interrupt_response(
        _ask_user_answer_request("tcid#1")
    ) is True


@pytest.mark.asyncio
async def test_guard_allows_live_card_answer() -> None:
    """当前活卡应答放行（死卡判定不命中；_instance=None → 相位检查 fail-open）。"""
    adapter = _guard_adapter(
        card_instances={"tcid#2": "tcid"},
        live_instances={"tcid": "tcid#2"},
    )
    assert await adapter.guard_stale_interrupt_response(
        _ask_user_answer_request("tcid#2")
    ) is False


class _MemSession:
    """相位读写用的内存 session，不碰 checkpointer。"""

    def __init__(self, phase: str | None = None) -> None:
        self._state: dict[str, object] = {}
        if phase is not None:
            self._state[INTERRUPT_PHASE_SESSION_KEY] = {"phase": phase}

    def get_state(self, key: str):
        return self._state.get(key)

    def update_state(self, patch: dict) -> None:
        self._state.update(patch)


def _permission_card(request_id: str, question: str) -> dict:
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "source": "permission_interrupt",
        "questions": [{"question": question, "options": ["本次允许"]}],
    }


def _permission_answer(card_id: str) -> AgentRequest:
    return AgentRequest(
        request_id="req-allow",
        channel_id="officeclaw",
        session_id="sess-guard",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "source": "permission_interrupt",
            "request_id": card_id,
            "answers": [{"selected_options": ["本次允许"]}],
        },
    )


def _card_adapter(phase: str | None) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._hitl_card_instances = {}
    adapter._hitl_base_live_instance = {}
    adapter._hitl_dead_card_ids = set()
    adapter._ask_user_card_seq = {}
    adapter._instance = SimpleNamespace(
        _interaction_session=_MemSession(phase),
        loop_session=None,
    )
    return adapter


def _emit_second_permission_card(adapter: JiuWenSwarmDeepAdapter) -> None:
    emitted_ids: set[str] = set()
    emitted_questions: dict[str, str] = {}
    first = _permission_card("call_x", "列出目录?")
    assert adapter._dedupe_ask_user_card(first, emitted_ids, emitted_questions) is False
    second = _permission_card("call_x", "列出目录? 新参数")
    assert adapter._dedupe_ask_user_card(second, emitted_ids, emitted_questions) is False
    assert second["request_id"] == "call_x#2"


@pytest.mark.asyncio
async def test_superseded_permission_card_rejected_while_live_suffix_resumes() -> None:
    """发出 #2 后，旧卡 call_x 拒绝；进行中的 call_x#2 仍放行。"""
    adapter = _card_adapter(None)
    _emit_second_permission_card(adapter)
    assert await adapter.guard_stale_interrupt_response(
        _permission_answer("call_x")
    ) is True
    # 活卡未进死卡集；相位检查需要真实 session，这里只验证卡片守卫放行。
    adapter._instance = None
    assert await adapter.guard_stale_interrupt_response(
        _permission_answer("call_x#2")
    ) is False


@pytest.mark.asyncio
async def test_finished_round_rejects_late_live_permission_card() -> None:
    """命令跑完、相位 resumed→idle 后，最后那张活卡的迟到允许被拒绝。"""
    adapter = _card_adapter(None)
    _emit_second_permission_card(adapter)
    # 发卡把相位打成 paused；应答受理后才是 resumed，轮次结束才回 idle。
    session = adapter._instance._interaction_session
    session.update_state({INTERRUPT_PHASE_SESSION_KEY: {"phase": PHASE_RESUMED}})
    adapter._mark_interrupt_idle_inmemory()
    session = adapter._instance._interaction_session
    phase = session.get_state(INTERRUPT_PHASE_SESSION_KEY)
    assert phase["phase"] == PHASE_IDLE
    assert "call_x#2" in adapter._hitl_dead_card_ids
    assert await adapter.guard_stale_interrupt_response(
        _permission_answer("call_x#2")
    ) is True


@pytest.mark.asyncio
async def test_paused_round_keeps_live_permission_card() -> None:
    """还有卡在等时不转死卡，进行中的 #2 仍可恢复。"""
    adapter = _card_adapter(None)
    _emit_second_permission_card(adapter)
    adapter._mark_interrupt_idle_inmemory()
    assert "call_x#2" not in adapter._hitl_dead_card_ids
    adapter._instance = None
    assert await adapter.guard_stale_interrupt_response(
        _permission_answer("call_x#2")
    ) is False


def test_missing_instance_does_not_invalidate_live_card() -> None:
    """读不到运行实例时不转死卡，避免重启后吞掉仍在等待的点击。"""
    adapter = _card_adapter(None)
    _emit_second_permission_card(adapter)
    session = adapter._instance._interaction_session
    session.update_state({INTERRUPT_PHASE_SESSION_KEY: {"phase": PHASE_RESUMED}})
    adapter._instance = None
    adapter._mark_interrupt_idle_inmemory()
    assert "call_x#2" not in adapter._hitl_dead_card_ids


@pytest.mark.asyncio
async def test_guard_fail_open_for_unregistered_card() -> None:
    """未登记卡片（老会话/权限卡）：fail-open，由相位守卫裁决。"""
    adapter = _guard_adapter()
    assert await adapter.guard_stale_interrupt_response(
        _ask_user_answer_request("unknown-card")
    ) is False


def test_invalidate_all_without_registry_is_noop() -> None:
    """object.__new__ 构造（注册表未初始化）：终态 invalidate 不抛异常。"""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._invalidate_all_hitl_card_instances()


# ────────────────── ask_user 应答识别 ──────────────────


def test_ask_user_interrupt_response_detected() -> None:
    request = AgentRequest(
        request_id="req-a",
        channel_id="officeclaw",
        session_id="sess-a",
        req_method=ReqMethod.CHAT_RESUME,
        params={"source": "ask_user_interrupt", "answers": [{"id": "x", "answer": "y"}]},
    )
    assert _is_ask_user_interrupt_response(request) is True


def test_plain_message_is_not_ask_user_response() -> None:
    request = AgentRequest(
        request_id="req-b",
        channel_id="officeclaw",
        session_id="sess-b",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "新任务"},
    )
    assert _is_ask_user_interrupt_response(request) is False


def test_permission_continuation_is_not_ask_user_response() -> None:
    """permission continuation（无 ask_user source）不走 ask_user 守卫分支。"""
    request = AgentRequest(
        request_id="req-c",
        channel_id="officeclaw",
        session_id="sess-c",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "source": "permission_interrupt",
            "request_id": "tcid-1",
            "answers": [{"id": "tcid-1", "answer": "allow"}],
        },
    )
    assert _is_ask_user_interrupt_response(request) is False
