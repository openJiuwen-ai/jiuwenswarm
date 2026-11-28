# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``call_goal`` schema injection and argument extraction.

``call_goal`` is produced by the model and rendered by every channel, so the
injected schema must pin the output language to the current UI / preferred
language (``zh`` / ``en``), not to the conversation or skill-document language.
"""

from __future__ import annotations

import json

from jiuwenswarm.common.tool_display import (
    call_goal_schema_for_language,
    extract_call_goal,
    inject_call_goal_schema,
    is_our_call_goal_schema,
)


def _injected_description(language: str | None = None) -> str:
    parameters: dict = {"type": "object", "properties": {}}
    inject_call_goal_schema(parameters, language=language)
    return str(parameters["properties"]["call_goal"]["description"])


class TestCallGoalSchemaLanguage:

    @staticmethod
    def test_default_language_is_english():
        """未传 language 时默认英文，避免无 UI 上下文时落到中文。"""
        description = _injected_description()
        assert "Always fill this field" in description
        assert "Write it in English to match the current UI language" in description

    @staticmethod
    def test_english_schema_asks_for_english_ui_language():
        description = _injected_description("en")
        assert "Always fill this field" in description
        assert "Write it in English to match the current UI language" in description
        assert "Research the openJiuwen website" in description
        assert "请用中文写" not in description

    @staticmethod
    def test_chinese_schema_asks_for_chinese_ui_language():
        description = _injected_description("zh")
        assert "每次调用都必须填写" in description
        assert "请用中文写" in description
        assert "调研 openJiuwen 官网信息" in description
        assert "Always fill this field" not in description

    @staticmethod
    def test_cn_alias_maps_to_chinese_schema():
        description = _injected_description("cn")
        assert "请用中文写" in description

    @staticmethod
    def test_description_keeps_the_display_name_disambiguation():
        """仍需保留与 display_name / summary 的区分说明，避免撞名回归。"""
        for language in ("en", "zh"):
            description = _injected_description(language)
            assert "display_name" in description
            assert "send_message.summary" in description

    @staticmethod
    def test_injection_still_adds_an_optional_string_field():
        """注入形状不变：可选 string 字段，不进 required。"""
        parameters: dict = {"type": "object", "properties": {}, "required": []}
        inject_call_goal_schema(parameters, language="en")
        assert parameters["properties"]["call_goal"]["type"] == "string"
        assert parameters["required"] == []

    @staticmethod
    def test_injection_does_not_overwrite_an_existing_tool_owned_call_goal():
        """工具自带 call_goal 定义时不覆盖。"""
        own = {"type": "string", "description": "tool own"}
        parameters: dict = {"type": "object", "properties": {"call_goal": own}}
        inject_call_goal_schema(parameters, language="en")
        assert parameters["properties"]["call_goal"] is own

    @staticmethod
    def test_injection_refreshes_our_schema_when_ui_language_changes():
        """我们注入过的 call_goal 应按新的 UI 语言刷新 description。"""
        parameters: dict = {"type": "object", "properties": {}}
        inject_call_goal_schema(parameters, language="zh")
        assert "请用中文写" in parameters["properties"]["call_goal"]["description"]
        inject_call_goal_schema(parameters, language="en")
        assert "Write it in English to match the current UI language" in parameters["properties"]["call_goal"]["description"]

    @staticmethod
    def test_is_our_call_goal_schema_detects_injected_descriptions():
        assert is_our_call_goal_schema(call_goal_schema_for_language("en"))
        assert is_our_call_goal_schema(call_goal_schema_for_language("zh"))
        assert not is_our_call_goal_schema({"type": "string", "description": "tool own"})


class TestExtractCallGoal:

    @staticmethod
    def test_strips_call_goal_from_dict_arguments():
        goal, cleaned = extract_call_goal(
            {"path": "/workspace/foo.py", "call_goal": "调研 openJiuwen 官网信息"}
        )
        assert goal == "调研 openJiuwen 官网信息"
        assert cleaned == {"path": "/workspace/foo.py"}
        assert "call_goal" not in cleaned

    @staticmethod
    def test_accepts_camel_case_call_goal():
        goal, cleaned = extract_call_goal({"path": "a.py", "callGoal": "Read file"})
        assert goal == "Read file"
        assert cleaned == {"path": "a.py"}

    @staticmethod
    def test_empty_call_goal_returns_empty_string():
        goal, cleaned = extract_call_goal({"path": "a.py"})
        assert goal == ""
        assert cleaned == {"path": "a.py"}

    @staticmethod
    def test_preserves_business_display_name():
        """工具业务参数里的 display_name 不受影响。"""
        goal, cleaned = extract_call_goal(
            {
                "display_name": "研究专家",
                "call_goal": "创建调研成员",
            }
        )
        assert goal == "创建调研成员"
        assert cleaned == {"display_name": "研究专家"}

    @staticmethod
    def test_strips_call_goal_from_json_string_arguments():
        raw = json.dumps(
            {"path": "/tmp/x", "call_goal": "读文件"},
            ensure_ascii=False,
        )
        goal, cleaned = extract_call_goal(raw)
        assert goal == "读文件"
        assert json.loads(cleaned) == {"path": "/tmp/x"}
