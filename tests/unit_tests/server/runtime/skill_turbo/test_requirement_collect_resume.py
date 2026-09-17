# -*- coding: utf-8 -*-
"""P2 HITL resume：P2.1 不覆盖已确认槽位；P2.2/P2.3 skip 条件。"""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import requirement_collect as rc


def test_merge_slot_payload_preserves_user_confirmed_page_count():
    inputs = {
        "page_count": 5,
        "page_count_user_specified": True,
        "audience": "历史课学生",
        "presentation_purpose": "教学分享",
        "need_ask_style": True,
        "missing_fields": ["style_id"],
    }
    payload = {
        "page_count": None,
        "page_count_user_specified": False,
        "audience": "其他受众",
        "presentation_purpose": "工作汇报",
        "style_id": "",
        "need_ask_style": True,
        "missing_fields": ["page_count", "style_id"],
    }
    rc._merge_slot_payload(inputs, payload, preserve_topic=True)
    assert inputs["page_count"] == 5
    assert inputs["page_count_user_specified"] is True
    assert inputs["audience"] == "历史课学生"
    assert inputs["presentation_purpose"] == "教学分享"
    assert "page_count" not in inputs["missing_fields"]
    assert "style_id" in inputs["missing_fields"]


def test_merge_slot_payload_preserves_resolved_style():
    inputs = {
        "topic": "历史文化介绍",
        "page_count": 6,
        "page_count_user_specified": True,
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
        "style_id": "elegant-narrative",
        "need_ask_style": False,
    }
    payload = {
        "page_count": None,
        "page_count_user_specified": False,
        "style_id": "",
        "need_ask_style": True,
        "missing_fields": ["page_count", "style_id"],
    }
    rc._merge_slot_payload(inputs, payload, preserve_topic=True)
    assert inputs["style_id"] == "elegant-narrative"
    assert inputs["need_ask_style"] is False
    assert inputs["missing_fields"] == []


def test_p21_should_skip_when_slots_analyzed_and_topic_present():
    inputs = {
        "requirement_collect_status": "slots_analyzed",
        "topic": "历史文化介绍",
    }
    assert rc._p21_should_skip(inputs) is True


def test_p21_should_not_skip_without_status():
    inputs = {"topic": "历史文化介绍"}
    assert rc._p21_should_skip(inputs) is False


def test_p21_should_not_skip_without_topic():
    inputs = {"requirement_collect_status": "slots_analyzed", "topic": ""}
    assert rc._p21_should_skip(inputs) is False


def test_batch_fields_need_ask_false_when_page_confirmed():
    inputs = {
        "page_count": 5,
        "page_count_user_specified": True,
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
    }
    assert rc._batch_fields_need_ask(inputs) is False


def test_reconcile_missing_fields_after_page_answer():
    inputs = {
        "topic": "历史文化介绍",
        "page_count": 5,
        "page_count_user_specified": True,
        "audience": "普通受众",
        "presentation_purpose": "教学分享",
        "need_ask_style": True,
        "missing_fields": ["page_count", "style_id"],
    }
    rc._reconcile_missing_fields(inputs)
    assert inputs["missing_fields"] == ["style_id"]
