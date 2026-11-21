# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the Playwright MCP argument (de)serialization helpers."""

import pytest

from jiuwenswarm.common.playwright_mcp_runtime import (
    parse_playwright_mcp_args,
    serialize_playwright_mcp_args,
)


class TestParsePlaywrightMcpArgs:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_or_blank_returns_empty_list(self, value):
        assert parse_playwright_mcp_args(value) == []

    def test_json_list_is_parsed(self):
        assert parse_playwright_mcp_args('["--port", "8080"]') == ["--port", "8080"]

    def test_json_list_items_are_stringified(self):
        assert parse_playwright_mcp_args("[1, 2]") == ["1", "2"]

    def test_json_list_with_embedded_spaces(self):
        assert parse_playwright_mcp_args('["a b", "c"]') == ["a b", "c"]

    def test_invalid_json_falls_back_to_shell_split(self):
        # shlex still applies quote rules to the non-JSON string.
        assert parse_playwright_mcp_args("['a', 'b']") == ["[a,", "b]"]

    def test_shell_style_string_is_split(self):
        assert parse_playwright_mcp_args("--port 8080 --headless") == [
            "--port",
            "8080",
            "--headless",
        ]

    def test_shell_quoting_preserves_spaces(self):
        assert parse_playwright_mcp_args('--path "a b c"') == ["--path", "a b c"]


class TestSerializePlaywrightMcpArgs:
    def test_json_round_trip(self):
        args = ["--port", "8080", "a b"]
        assert parse_playwright_mcp_args(serialize_playwright_mcp_args(args)) == args

    def test_empty_list_serializes(self):
        assert serialize_playwright_mcp_args([]) == "[]"

    def test_non_string_items_are_stringified(self):
        assert serialize_playwright_mcp_args([1, 2]) == '["1", "2"]'
