# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""同一外层 skill_acceleration_exec 调用内连续 ask_user 中断的卡片区分。

回退强制 emit 后卡片唯一来源是 harness __interaction__，request_id = 外层
call id——同一外层调用内第二次中断与第一次共用同一 id，前端按 requestId
区分卡片会丢卡；且流内去重会把它当重复跳过。此处验证：
- 第二次中断（同 id 不同 questions）→ 卡片 id 加序号后缀 {id}#{n} 放行
- 同一中断的多通道重复（同 id 同 questions）→ 跳过
- 序号计数跨请求存续（新流内集合不误判、id 不回退）
"""

from __future__ import annotations

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

_Q_P1 = [{"question": "演示主题是什么？", "header": "主题", "options": []}]
_Q_P2 = [{"question": "需要多少页？", "header": "页数", "options": []}]


def _card(request_id: str, questions: list) -> dict:
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": questions,
        "source": "ask_user_interrupt",
    }


def _adapter() -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._ask_user_card_seq = {}
    return adapter


def test_first_interrupt_uses_raw_id() -> None:
    adapter = _adapter()
    ids: set[str] = set()
    questions: dict[str, str] = {}
    card = _card("call_outer_1", _Q_P1)
    assert adapter._dedupe_ask_user_card(card, ids, questions) is False
    assert card["request_id"] == "call_outer_1"


def test_second_interrupt_same_outer_id_gets_suffix() -> None:
    """同一外层调用内第二次中断（不同 questions）：id 加 #2 后缀放行。"""
    adapter = _adapter()
    ids: set[str] = set()
    questions: dict[str, str] = {}
    first = _card("call_outer_1", _Q_P1)
    second = _card("call_outer_1", _Q_P2)
    assert adapter._dedupe_ask_user_card(first, ids, questions) is False
    assert adapter._dedupe_ask_user_card(second, ids, questions) is False
    assert first["request_id"] == "call_outer_1"
    assert second["request_id"] == "call_outer_1#2"


def test_same_interrupt_duplicate_channels_skipped() -> None:
    """同一中断的多通道重复（同 id 同 questions）：跳过，不改 id。"""
    adapter = _adapter()
    ids: set[str] = set()
    questions: dict[str, str] = {}
    first = _card("call_outer_1", _Q_P1)
    replay = _card("call_outer_1", _Q_P1)
    assert adapter._dedupe_ask_user_card(first, ids, questions) is False
    assert adapter._dedupe_ask_user_card(replay, ids, questions) is True
    assert replay["request_id"] == "call_outer_1"


def test_duplicate_channel_of_second_card_also_skipped() -> None:
    """第二张卡（#2）的迟到重复通道：不误判为新卡、不弹副本。"""
    adapter = _adapter()
    ids: set[str] = set()
    questions: dict[str, str] = {}
    for card in (
        _card("call_outer_1", _Q_P1),
        _card("call_outer_1", _Q_P2),
    ):
        assert adapter._dedupe_ask_user_card(card, ids, questions) is False
    dup_of_second = _card("call_outer_1", _Q_P2)
    assert adapter._dedupe_ask_user_card(dup_of_second, ids, questions) is True


def test_seq_survives_across_requests() -> None:
    """序号跨请求存续：新流（空集合）内第二次中断仍得 #2，不回退原 id。"""
    adapter = _adapter()
    ids1: set[str] = set()
    questions1: dict[str, str] = {}
    assert adapter._dedupe_ask_user_card(
        _card("call_outer_1", _Q_P1), ids1, questions1
    ) is False
    # 新请求：流内集合为空，adapter 实例（seq）存续
    ids2: set[str] = set()
    questions2: dict[str, str] = {}
    second = _card("call_outer_1", _Q_P2)
    assert adapter._dedupe_ask_user_card(second, ids2, questions2) is False
    assert second["request_id"] == "call_outer_1#2"


def test_third_interrupt_gets_next_suffix() -> None:
    adapter = _adapter()
    ids: set[str] = set()
    questions: dict[str, str] = {}
    q3 = [{"question": "风格？", "header": "风格", "options": []}]
    for card in (_card("call_outer_1", _Q_P1), _card("call_outer_1", _Q_P2)):
        adapter._dedupe_ask_user_card(card, ids, questions)
    third = _card("call_outer_1", q3)
    assert adapter._dedupe_ask_user_card(third, ids, questions) is False
    assert third["request_id"] == "call_outer_1#3"
