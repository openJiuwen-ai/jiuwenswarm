# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for _sanitize_glm_tool_xml_tags in llm_sse_patch."""

from jiuwenswarm.llm_sse_patch import _sanitize_glm_tool_xml_tags


class TestSanitizeGlmToolXmlTags:

    @staticmethod
    def test_empty_string():
        assert _sanitize_glm_tool_xml_tags("") == ""

    @staticmethod
    def test_no_tags_unchanged():
        raw = "plain text without any tags"
        assert _sanitize_glm_tool_xml_tags(raw) == raw

    @staticmethod
    def test_early_exit_no_match():
        raw = "text with no arg_ or tool_call markers"
        assert _sanitize_glm_tool_xml_tags(raw) == raw

    @staticmethod
    def test_truncated_tool_call_open_tag():
        raw = "prefix<tool_call>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "suffix"

    @staticmethod
    def test_truncated_arg_value_open_tag():
        raw = "prefix<arg_value>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "suffix"

    @staticmethod
    def test_truncated_arg_key_open_tag():
        raw = "prefix<arg_key>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "suffix"

    @staticmethod
    def test_closed_tool_call_tag_removed():
        raw = "prefix<tool_call>content</tool_call>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "prefixsuffix"

    @staticmethod
    def test_closed_arg_value_tag_removed():
        raw = "prefix<arg_value>inner</arg_value>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "prefixsuffix"

    @staticmethod
    def test_closed_arg_key_tag_removed():
        raw = "prefix<arg_key>inner</arg_key>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "prefixsuffix"

    @staticmethod
    def test_closed_tag_with_attributes():
        raw = "prefix<tool_call name='fn'>inner</tool_call>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "prefixsuffix"

    @staticmethod
    def test_closed_tag_multiline_content():
        raw = "prefix<arg_value>\nline1\nline2\n</arg_value>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "prefixsuffix"

    @staticmethod
    def test_multiple_closed_tags():
        raw = "start<arg_value>v1</arg_value>mid<arg_key>k1</arg_key>end"
        assert _sanitize_glm_tool_xml_tags(raw) == "startmidend"

    @staticmethod
    def test_only_closed_tag_no_surrounding():
        raw = "<arg_value>content</arg_value>"
        assert _sanitize_glm_tool_xml_tags(raw) == ""

    @staticmethod
    def test_self_closing_style_tag_strips_prefix():
        raw = "prefix<arg_value />suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "suffix"

    @staticmethod
    def test_closed_then_truncated_leaves_only_tail():
        raw = "start<arg_value>removed</arg_value>mid<tool_call>tail"
        assert _sanitize_glm_tool_xml_tags(raw) == "tail"

    @staticmethod
    def test_only_truncated_open_tag_no_suffix():
        raw = "prefix<tool_call>"
        assert _sanitize_glm_tool_xml_tags(raw) == ""

    @staticmethod
    def test_uppercase_tool_call_is_stripped():
        raw = "prefix<TOOL_CALL>inner</TOOL_CALL>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "prefixsuffix"

    @staticmethod
    def test_mixed_arg_and_tool_call_tags():
        raw = "a<arg_value>x</arg_value>b<arg_key>y</arg_key>c"
        assert _sanitize_glm_tool_xml_tags(raw) == "abc"

    @staticmethod
    def test_idempotent():
        raw = "prefix<arg_value>v</arg_value>suffix"
        first = _sanitize_glm_tool_xml_tags(raw)
        second = _sanitize_glm_tool_xml_tags(first)
        assert first == second

    @staticmethod
    def test_chinese_text_with_closed_tags():
        raw = "正在分析<arg_value>分析过程</arg_value>分析完成"
        assert _sanitize_glm_tool_xml_tags(raw) == "正在分析分析完成"

    @staticmethod
    def test_real_world_active_form():
        raw = "执行中<arg_key>step</arg_key>已完成"
        assert _sanitize_glm_tool_xml_tags(raw) == "执行中已完成"

    @staticmethod
    def test_real_world_tool_arguments():
        raw = '{"query": "<arg_value>search_term</arg_value>"}'
        expected = '{"query": ""}'
        assert _sanitize_glm_tool_xml_tags(raw) == expected

    @staticmethod
    def test_nested_closed_tags_removed_entirely():
        # Outer tool_call tag with inner arg_value: back-reference ensures
        # closing tag name matches opening tag name, so the entire outer
        # block is removed without leaving a stray closing tag.
        raw = "<tool_call><arg_value>deep</arg_value></tool_call>"
        assert _sanitize_glm_tool_xml_tags(raw) == ""

    @staticmethod
    def test_nested_closed_tags_with_prefix_suffix():
        raw = "prefix<tool_call><arg_value>deep</arg_value></tool_call>suffix"
        assert _sanitize_glm_tool_xml_tags(raw) == "prefixsuffix"

    @staticmethod
    def test_two_truncated_open_tags():
        raw = "prefix<arg_value>middle<tool_call>suffix"
        # Both truncated tags and their preceding text are removed iteratively:
        # Iteration 1: remove prefix + first open tag -> middle + second tag + suffix
        # Iteration 2: remove middle + second open tag -> suffix
        assert _sanitize_glm_tool_xml_tags(raw) == "suffix"

    @staticmethod
    def test_two_truncated_open_tags_no_prefix():
        raw = "<arg_value>text<tool_call>more"
        assert _sanitize_glm_tool_xml_tags(raw) == "more"

    @staticmethod
    def test_three_truncated_open_tags():
        raw = "<arg_value><arg_key><tool_call>final"
        assert _sanitize_glm_tool_xml_tags(raw) == "final"

    @staticmethod
    def test_closed_then_two_truncated_tags():
        raw = "start<arg_value>removed</arg_value>mid<arg_key>tail<tool_call>end"
        assert _sanitize_glm_tool_xml_tags(raw) == "end"

    @staticmethod
    def test_idempotent_with_multiple_truncated_tags():
        raw = "prefix<arg_value>middle<tool_call>suffix"
        first = _sanitize_glm_tool_xml_tags(raw)
        second = _sanitize_glm_tool_xml_tags(first)
        assert first == second
