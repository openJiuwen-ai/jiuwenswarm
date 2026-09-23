# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P4 批量卡合并测试。

覆盖：
1. 合并候选判定（``read_hitl_batch_merge_key``）：仅带 auto_confirm_key 的
   权限/确认类 ``__interaction__`` 单 payload chunk 可合并；ask_user /
   activate_confirm / 列表 payload / 无 id 均排除。
2. 排空窗包装器（``_merge_batch_interaction_stream``）：同 key 分组合并、
   多 key 分组独立、size==1 原样透传、非候选回推保序、超时不取消 anext
   任务（不丢 chunk）、流结束 flush。
3. 列表 payload 批量卡（``_parse_stream_chunk`` + ``annotate_hitl_batch_card``）：
   batch_size / ×N 文案 / 成员注册。
4. 答案展开（``_build_interactive_input_from_answers``）：注册表命中时对
   批内每个成员 update；未命中时单成员（零行为变化）。
5. 注册表生命周期：record/peek/discard/容量上限；非批量卡顶替时作废
   （``_dedupe_ask_user_card`` 清理钩子）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    _HITL_BATCH_MEMBERS,
    _HITL_BATCH_MEMBERS_MAX,
    annotate_hitl_batch_card,
    discard_hitl_batch_member_entry,
    peek_hitl_batch_members,
    read_hitl_batch_merge_key,
    record_hitl_batch_members,
)
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from openjiuwen.core.single_agent.interrupt.response import ToolCallInterruptRequest


@pytest.fixture(autouse=True)
def _clean_batch_registry():
    """注册表是模块级共享状态，测试间隔离。"""
    _HITL_BATCH_MEMBERS.clear()
    yield
    _HITL_BATCH_MEMBERS.clear()


# ────────────────── 构造工具 ──────────────────


def _perm_value(call_id: str, key: str = "web_search") -> ToolCallInterruptRequest:
    return ToolCallInterruptRequest(
        message="允许执行 web_search?",
        tool_name="web_search",
        tool_call_id=call_id,
        tool_args={"query": "x"},
        auto_confirm_key=key,
    )


def _perm_chunk(call_id: str, key: str = "web_search") -> SimpleNamespace:
    return SimpleNamespace(
        type="__interaction__",
        payload=SimpleNamespace(id=call_id, value=_perm_value(call_id, key=key)),
    )


def _plain_chunk() -> SimpleNamespace:
    return SimpleNamespace(type="llm_output", payload={"content": "hi"})


async def _collect(stream) -> list[Any]:
    return [chunk async for chunk in stream]


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


# ────────────────── 1. 合并候选判定 ──────────────────


def test_read_merge_key_classifies_chunks() -> None:
    assert read_hitl_batch_merge_key(_perm_chunk("c-1")) == "web_search"

    # 非 __interaction__ chunk
    assert read_hitl_batch_merge_key(_plain_chunk()) is None
    # payload 为 None / 列表（已合并形态）
    assert (
        read_hitl_batch_merge_key(SimpleNamespace(type="__interaction__", payload=None))
        is None
    )
    assert (
        read_hitl_batch_merge_key(
            SimpleNamespace(type="__interaction__", payload=[_perm_value("c-1")])
        )
        is None
    )
    # activate_confirm
    assert (
        read_hitl_batch_merge_key(
            SimpleNamespace(
                type="__interaction__",
                payload={"id": "i-1", "interaction_type": "activate_confirm"},
            )
        )
        is None
    )
    # 无 id
    assert (
        read_hitl_batch_merge_key(
            SimpleNamespace(
                type="__interaction__",
                payload=SimpleNamespace(id="", value=_perm_value("c-1")),
            )
        )
        is None
    )
    # ask_user（value 带 questions）排除
    ask_value = ToolCallInterruptRequest(
        message="q",
        tool_name="ask_user",
        tool_call_id="c-2",
        tool_args={"questions": [{"question": "选哪个?", "header": "h", "options": []}]},
        auto_confirm_key="ask_user:c-2",
    )
    assert (
        read_hitl_batch_merge_key(
            SimpleNamespace(
                type="__interaction__",
                payload=SimpleNamespace(id="c-2", value=ask_value),
            )
        )
        is None
    )
    # 无 auto_confirm_key → 回退按工具上下文计算（真实链路 PermissionEngine
    # 发卡不带 key，实测纠偏：web_search 回退为 'web_search'）
    no_key_value = ToolCallInterruptRequest(
        message="允许?",
        tool_name="web_search",
        tool_call_id="c-3",
        tool_args={},
    )
    assert (
        read_hitl_batch_merge_key(
            SimpleNamespace(
                type="__interaction__",
                payload=SimpleNamespace(id="c-3", value=no_key_value),
            )
        )
        == "web_search"
    )
    # 无 key 且无 tool_name → 不合并
    no_ctx_value = ToolCallInterruptRequest(message="确认?", tool_call_id="c-4")
    assert (
        read_hitl_batch_merge_key(
            SimpleNamespace(
                type="__interaction__",
                payload=SimpleNamespace(id="c-4", value=no_ctx_value),
            )
        )
        is None
    )
    # 无 key 的 bash 简单命令 → 回退 bash:subcommand 粒度（与 batch allow 同键）
    bash_value = ToolCallInterruptRequest(
        message="允许执行 bash?",
        tool_name="bash",
        tool_call_id="c-5",
        tool_args={"command": "ls -la"},
    )
    assert (
        read_hitl_batch_merge_key(
            SimpleNamespace(
                type="__interaction__",
                payload=SimpleNamespace(id="c-5", value=bash_value),
            )
        )
        == "bash:ls -la"
    )


async def test_merge_stream_batches_no_key_real_shape() -> None:
    """实测纠偏回归：真实链路卡（无 auto_confirm_key、有 tool_name）也合并。"""

    async def source():
        for i in range(3):
            value = ToolCallInterruptRequest(
                message="允许执行 web_search?",
                tool_name="web_search",
                tool_call_id=f"call_{i}",
                tool_args={"query": "x"},
            )
            yield SimpleNamespace(
                type="__interaction__",
                payload=SimpleNamespace(id=f"call_{i}", value=value),
            )

    adapter = _make_adapter()
    chunks = await _collect(adapter._merge_batch_interaction_stream(source()))
    assert len(chunks) == 1
    batch = chunks[0]
    assert isinstance(batch.payload, list)
    assert len(batch.payload) == 3


# ────────────────── 2. 排空窗包装器 ──────────────────


async def test_merge_stream_batches_same_key() -> None:
    """同 key 背靠背到达 → 合成一张列表 payload chunk（成员序保持）。"""

    async def source():
        yield _perm_chunk("c-1")
        yield _perm_chunk("c-2")
        yield _perm_chunk("c-3")

    adapter = _make_adapter()
    out = await _collect(adapter._merge_batch_interaction_stream(source()))

    assert len(out) == 1
    merged = out[0]
    assert merged.type == "__interaction__"
    assert isinstance(merged.payload, list)
    assert [p.id for p in merged.payload] == ["c-1", "c-2", "c-3"]


async def test_merge_stream_single_group_passthrough_keeps_object() -> None:
    """size==1 的组原对象透传（零行为变化）；非候选保序。"""

    async def source():
        yield _perm_chunk("c-1")
        yield _plain_chunk()

    adapter = _make_adapter()
    out = await _collect(adapter._merge_batch_interaction_stream(source()))

    assert len(out) == 2
    assert out[0].type == "__interaction__"
    assert not isinstance(out[0].payload, list)  # 单成员不合成
    assert out[1].type == "llm_output"


async def test_merge_stream_separate_keys_form_separate_batches() -> None:
    """同窗多 key 分组独立：A×2 合并、B×1 透传，输出保持到达序。"""

    async def source():
        yield _perm_chunk("a-1", key="web_search")
        yield _perm_chunk("b-1", key="bash:git status")
        yield _perm_chunk("a-2", key="web_search")

    adapter = _make_adapter()
    out = await _collect(adapter._merge_batch_interaction_stream(source()))

    assert len(out) == 2
    first, second = out
    # 首组（a）合并
    assert first.type == "__interaction__"
    assert [p.id for p in first.payload] == ["a-1", "a-2"]
    # 次组（b）单成员透传
    assert second.type == "__interaction__"
    assert not isinstance(second.payload, list)


async def test_merge_stream_timeout_keeps_task_without_chunk_loss() -> None:
    """超时关闭窗口后 anext 任务保留：后续 chunk 不丢失、不挂起。

    若实现误用 wait_for（超时取消任务），第二个 chunk 会随取消被丢弃。
    """

    async def source():
        yield _perm_chunk("c-1")
        await asyncio.sleep(0.35)  # > 排空窗超时 0.2s
        yield _perm_chunk("c-2")

    adapter = _make_adapter()
    out = await _collect(adapter._merge_batch_interaction_stream(source()))

    assert len(out) == 2
    assert out[0].type == "__interaction__"
    assert out[1].type == "__interaction__"
    assert out[1].payload.value.tool_call_id == "c-2"  # 未丢


async def test_merge_stream_flushes_pending_batch_on_stream_end() -> None:
    """流在窗口内结束：已收集组照常合成（StopAsyncIteration flush 路径）。"""

    async def source():
        yield _perm_chunk("c-1")
        yield _perm_chunk("c-2")

    adapter = _make_adapter()
    out = await _collect(adapter._merge_batch_interaction_stream(source()))

    assert len(out) == 1
    assert [p.id for p in out[0].payload] == ["c-1", "c-2"]


async def test_merge_stream_passes_plain_chunks_untouched() -> None:
    """纯普通流（无候选）逐 chunk 原对象透传。"""

    chunks = [_plain_chunk(), _plain_chunk()]

    async def source():
        for chunk in chunks:
            yield chunk

    adapter = _make_adapter()
    out = await _collect(adapter._merge_batch_interaction_stream(source()))

    assert out == chunks
    assert out[0] is chunks[0]
    assert out[1] is chunks[1]


# ────────────────── 3. 列表 payload 批量卡 ──────────────────


def _batch_chunk_payload(member_count: int = 3) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(id=f"c-{i}", value=_perm_value(f"c-{i}"))
        for i in range(1, member_count + 1)
    ]


def test_parse_chunk_list_payload_emits_batch_card() -> None:
    """列表 payload → 批量卡：首成员 id、×N 文案、batch_size、成员注册。"""
    payload = _batch_chunk_payload(3)
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        SimpleNamespace(type="__interaction__", payload=payload)
    )

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["source"] == "permission_interrupt"
    assert parsed["request_id"] == "c-1"  # 首成员 tool_call_id
    assert parsed["batch_size"] == 3
    question_text = parsed["questions"][0]["question"]
    assert "本批共 3 个" in question_text
    # 成员注册供应答侧展开
    assert peek_hitl_batch_members("c-1") == ("c-1", "c-2", "c-3")


def test_annotate_noop_for_invalid_shapes() -> None:
    """防御分支：非 dict 卡 / 非列表 payload / 非 permission 源 / 成员<2。"""
    card = {
        "event_type": "chat.ask_user_question",
        "request_id": "c-1",
        "source": "permission_interrupt",
        "questions": [{"question": "允许执行?"}],
    }

    # payload 非列表
    annotate_hitl_batch_card(card, _perm_chunk("c-1"))
    assert "batch_size" not in card
    assert peek_hitl_batch_members("c-1") is None

    # 成员 < 2
    annotate_hitl_batch_card(card, [SimpleNamespace(id="c-1", value=_perm_value("c-1"))])
    assert "batch_size" not in card
    assert peek_hitl_batch_members("c-1") is None

    # 非 permission/confirm 源
    ask_card = dict(card, source="ask_user_interrupt")
    annotate_hitl_batch_card(ask_card, _batch_chunk_payload(2))
    assert "batch_size" not in ask_card
    assert peek_hitl_batch_members("c-1") is None

    # 卡非 dict
    annotate_hitl_batch_card(None, _batch_chunk_payload(2))
    assert peek_hitl_batch_members("c-1") is None


def test_annotate_does_not_duplicate_suffix() -> None:
    """重复标注不叠加 ×N 文案。"""
    payload = _batch_chunk_payload(2)
    card = {
        "event_type": "chat.ask_user_question",
        "request_id": "c-1",
        "source": "permission_interrupt",
        "questions": [{"question": "允许执行?"}],
    }
    annotate_hitl_batch_card(card, payload)
    first_text = card["questions"][0]["question"]
    assert first_text.count("本批共") == 1
    annotate_hitl_batch_card(card, payload)
    assert card["questions"][0]["question"] == first_text


# ────────────────── 4. 答案展开 ──────────────────


def test_interactive_input_expands_batch_members() -> None:
    """批量卡答案展开：批内每个成员 tool_call_id 逐个 update。"""
    record_hitl_batch_members("c-1", ("c-1", "c-2", "c-3"))
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "c-1",
        [{"selected_options": ["approve"]}],
        "permission_interrupt",
    )
    assert set(interactive_input.user_inputs.keys()) == {"c-1", "c-2", "c-3"}
    assert all(v["approved"] is True for v in interactive_input.user_inputs.values())


def test_interactive_input_without_registry_single_update() -> None:
    """无批量注册时只 update 原请求 id（零行为变化）。"""
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "c-9",
        [{"selected_options": ["approve"]}],
        "permission_interrupt",
    )
    assert set(interactive_input.user_inputs.keys()) == {"c-9"}


def test_interactive_input_registry_survives_after_peek() -> None:
    """peek 不弹出：resume 失败后重答仍可展开。"""
    record_hitl_batch_members("c-1", ("c-1", "c-2"))
    JiuWenSwarm._build_interactive_input_from_answers(
        "c-1",
        [{"selected_options": ["approve"]}],
        "permission_interrupt",
    )
    assert peek_hitl_batch_members("c-1") == ("c-1", "c-2")


# ────────────────── 5. 注册表生命周期 ──────────────────


def test_registry_record_peek_discard() -> None:
    record_hitl_batch_members("c-1", ("c-1", "c-2"))
    assert peek_hitl_batch_members("c-1") == ("c-1", "c-2")
    discard_hitl_batch_member_entry("c-1")
    assert peek_hitl_batch_members("c-1") is None
    # 重复 discard 幂等
    discard_hitl_batch_member_entry("c-1")


def test_registry_capacity_bound() -> None:
    """容量上限：超出后按旧序淘汰，总量受 _HITL_BATCH_MEMBERS_MAX 约束。"""
    for i in range(_HITL_BATCH_MEMBERS_MAX + 100):
        record_hitl_batch_members(f"c-{i}", (f"c-{i}",))
    assert len(_HITL_BATCH_MEMBERS) <= _HITL_BATCH_MEMBERS_MAX


def test_dedupe_single_card_discards_stale_batch_registry() -> None:
    """非批量卡以同 base 发出时作废旧批量注册（防误展开）。"""
    record_hitl_batch_members("tc-1", ("tc-1", "tc-2"))
    adapter = _make_adapter()
    card = {
        "event_type": "chat.ask_user_question",
        "request_id": "tc-1",
        "source": "permission_interrupt",
        "questions": [{"question": "允许执行 web_search?"}],
    }
    assert adapter._dedupe_ask_user_card(card, set(), {}) is False
    assert peek_hitl_batch_members("tc-1") is None


def test_dedupe_batch_card_keeps_registry() -> None:
    """批量卡（带 batch_size）发出时保留注册（record 已在 annotate 覆盖）。"""
    record_hitl_batch_members("tc-1", ("tc-1", "tc-2"))
    adapter = _make_adapter()
    card = {
        "event_type": "chat.ask_user_question",
        "request_id": "tc-1",
        "source": "permission_interrupt",
        "batch_size": 2,
        "questions": [{"question": "允许执行 web_search?（本批共 2 个同类请求）"}],
    }
    assert adapter._dedupe_ask_user_card(card, set(), {}) is False
    assert peek_hitl_batch_members("tc-1") == ("tc-1", "tc-2")
