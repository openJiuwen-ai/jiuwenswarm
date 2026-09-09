# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the injected ``call_goal`` schema description.

``call_goal`` is produced by the model and rendered by every channel, so the
description injected into the tool schema must not pin its language: it has to
follow the conversation instead of mandating one language.
"""

from __future__ import annotations

from jiuwenswarm.common.tool_display import inject_call_goal_schema


def _injected_description() -> str:
    parameters: dict = {"type": "object", "properties": {}}
    inject_call_goal_schema(parameters)
    return str(parameters["properties"]["call_goal"]["description"])


class TestCallGoalSchemaLanguage:

    @staticmethod
    def test_description_does_not_mandate_chinese_output():
        """描述不得要求模型固定用中文产出 call_goal。"""
        description = _injected_description()
        assert "一句简短中文" not in description

    @staticmethod
    def test_description_ties_the_goal_to_the_conversation_language():
        """描述必须把 call_goal 的语言绑定到当前对话。"""
        description = _injected_description()
        assert "当前对话所用的语言" in description
        assert "same language as the conversation" in description

    @staticmethod
    def test_description_shows_a_non_chinese_example():
        """描述给出非中文示例，避免模型把中文示例当成语言要求。"""
        description = _injected_description()
        assert "Research the openJiuwen website" in description

    @staticmethod
    def test_description_keeps_the_display_name_disambiguation():
        """仍需保留与 display_name / summary 的区分说明，避免撞名回归。"""
        description = _injected_description()
        assert "display_name" in description
        assert "send_message.summary" in description

    @staticmethod
    def test_injection_still_adds_an_optional_string_field():
        """注入形状不变：可选 string 字段，不进 required。"""
        parameters: dict = {"type": "object", "properties": {}, "required": []}
        inject_call_goal_schema(parameters)
        assert parameters["properties"]["call_goal"]["type"] == "string"
        assert parameters["required"] == []

    @staticmethod
    def test_injection_does_not_overwrite_an_existing_call_goal():
        """工具自带 call_goal 定义时不覆盖。"""
        own = {"type": "string", "description": "tool own"}
        parameters: dict = {"type": "object", "properties": {"call_goal": own}}
        inject_call_goal_schema(parameters)
        assert parameters["properties"]["call_goal"] is own
