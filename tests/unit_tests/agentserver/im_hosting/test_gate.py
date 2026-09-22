from __future__ import annotations

import pytest

from jiuwenswarm.server.im.im_hosting.gate import (
    matches_keywords,
    message_passes_gate,
    resolve_rule,
    split_keywords,
)


def test_split_and_match_keywords():
    assert split_keywords("入职, 报销，环境") == ["入职", "报销", "环境"]
    assert matches_keywords("请问入职流程？", ["入职", "报销"])
    assert not matches_keywords("今晚聚餐", ["入职", "报销"])


def test_resolve_session_override_beats_global():
    target = {
        "target_kind": "group",
        "rule_override": {"match_mode": "relevant", "keywords": ["VPN"]},
    }
    policy = {
        "default_group_rule": {"match_mode": "keyword", "keywords": ["入职"]},
        "default_user_rule": {"match_mode": "relevant", "keywords": []},
    }
    rule = resolve_rule(target, policy)
    assert rule["match_mode"] == "relevant"
    assert rule["keywords"] == ["VPN"]


def test_empty_override_inherits_kind_default():
    target = {"target_kind": "user", "rule_override": None}
    policy = {
        "default_group_rule": {"match_mode": "keyword", "keywords": ["入职"]},
        "default_user_rule": {"match_mode": "relevant", "keywords": ["报销"]},
    }
    rule = resolve_rule(target, policy)
    assert rule["match_mode"] == "relevant"
    assert rule["keywords"] == ["报销"]


@pytest.mark.asyncio
async def test_keyword_gate_requires_hit():
    rule = {"match_mode": "keyword", "keywords": ["入职"]}
    ok, reason = await message_passes_gate("入职材料发哪里", rule)
    assert ok and reason == "keyword"
    miss, miss_reason = await message_passes_gate("今天天气", rule)
    assert not miss and miss_reason == "keyword_miss"


@pytest.mark.asyncio
async def test_relevant_uses_judge_and_needs_topic():
    rule = {"match_mode": "relevant", "keywords": ["环境配置"]}

    async def judge(text: str, keywords) -> bool:
        del keywords
        return "IDE" in text

    ok, reason = await message_passes_gate("IDE 打不开", rule, relevance_judge=judge)
    assert ok and reason == "relevant"
    miss, miss_reason = await message_passes_gate("吃啥", rule, relevance_judge=judge)
    assert not miss and miss_reason == "relevant_miss"
    empty, empty_reason = await message_passes_gate(
        "随便问一句",
        {"match_mode": "relevant", "keywords": []},
        relevance_judge=judge,
    )
    assert empty and empty_reason == "unfiltered"
    open_kw, open_reason = await message_passes_gate(
        "今晚吃饭",
        {"match_mode": "keyword", "keywords": []},
    )
    assert open_kw and open_reason == "unfiltered"
