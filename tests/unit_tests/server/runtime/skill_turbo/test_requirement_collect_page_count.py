# -*- coding: utf-8 -*-
"""P2.2 页数收集：用户未指定时必须 ask，soft floor 仅在 ask 之后生效。"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import requirement_collect as rc


def test_page_count_with_soft_default_still_needs_ask():
    """系统预填 page_count 但 user_specified=false 时，P2.2 仍应询问页数。"""
    inputs = {
        "page_count": 6,
        "page_count_user_specified": False,
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
    }
    assert rc._batch_field_is_satisfied(inputs, "page_count") is True
    assert rc._batch_field_needs_user_ask(inputs, "page_count") is True
    assert "page_count" in rc._unsatisfied_batch_fields_for_ask(inputs)
    assert rc._unsatisfied_batch_fields(inputs) == []


def test_page_count_user_specified_skips_ask():
    inputs = {
        "page_count": 8,
        "page_count_user_specified": True,
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
    }
    assert rc._batch_field_needs_user_ask(inputs, "page_count") is False
    assert "page_count" not in rc._unsatisfied_batch_fields_for_ask(inputs)


def test_p21_leaves_page_count_null_until_p22():
    """P2.1 合并槽位后不再调用 soft floor，page_count 保持 null 直至 P2.2。"""
    inputs = {
        "page_count": None,
        "page_count_user_specified": False,
        "user_structure": "封面、目录、历史背景、事件经过",
        "user_dimensions": [],
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
    }
    assert inputs["page_count"] is None
    assert "page_count" in rc._unsatisfied_batch_fields_for_ask(inputs)


def test_soft_floor_applied_after_p22_default():
    """P2.2 结束后：先兜底默认页数，再按 structure 抬升 soft floor。"""
    inputs = {
        "page_count": None,
        "page_count_user_specified": False,
        "user_structure": "封面、目录、历史背景、事件经过",
        "user_dimensions": [],
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
    }
    assert "page_count" in rc._unsatisfied_batch_fields_for_ask(inputs)

    inputs["page_count"] = rc._DEFAULT_PAGE_COUNT
    rc._apply_soft_page_count_floor(inputs)
    assert inputs["page_count"] == 6
    assert rc._unsatisfied_batch_fields(inputs) == []


def test_user_specified_page_count_not_raised_by_soft_floor():
    inputs = {
        "page_count": 4,
        "page_count_user_specified": True,
        "user_structure": "封面、目录、历史背景、事件经过",
    }
    rc._apply_soft_page_count_floor(inputs)
    assert inputs["page_count"] == 4


@pytest.mark.parametrize(
    "label,expected",
    [
        ("3-6 页（推荐）", 6),
        ("8-12 页", 10),
        ("15-20 页", 18),
    ],
)
def test_apply_answer_marks_page_count_user_specified(label: str, expected: int):
    inputs: dict = {"page_count_user_specified": False}
    rc._apply_answer_item(
        inputs,
        {"header": "页数", "selected_options": [label]},
    )
    assert inputs["page_count"] == expected
    assert inputs["page_count_user_specified"] is True
    assert "page_count" not in rc._unsatisfied_batch_fields_for_ask(inputs)


@pytest.mark.asyncio
async def test_llm_default_batch_marks_page_count_collected():
    """超时 LLM 兜底写完 page_count 后必须标记已收集，避免 resume 再问页数。"""
    class _Node:
        async def stream_llm_collect(self, *_args, **_kwargs):
            return '{"page_count": 10}'

    inputs = {
        "page_count": None,
        "page_count_user_specified": False,
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
        "user_query": "历史文化介绍 PPT",
    }
    await rc._llm_default_batch_fields(_Node(), inputs, ["page_count"])
    assert inputs["page_count"] == 10
    assert inputs["page_count_user_specified"] is True
    assert rc._batch_field_needs_user_ask(inputs, "page_count") is False
    assert rc._batch_fields_need_ask(inputs) is False
