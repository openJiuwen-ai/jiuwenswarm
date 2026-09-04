# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ask_user 校验失败在 wire 展示层的屏蔽逻辑单测。

规则：chat.tool_result 且 tool_name=ask_user、result 以 [INVALID_ARGUMENT] 开头时，
发往前端的 result 替换为 [已跳过]；模型侧详细 tool_result 不受影响。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _tool_result_chunk(result: str, tool_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_result",
        payload={
            "tool_result": {
                "result": result,
                "tool_name": tool_name,
                "tool_call_id": "call_test_001",
            }
        },
    )


def _parse(result: str, tool_name: str) -> dict:
    return JiuWenSwarmDeepAdapter._parse_stream_chunk(
        _tool_result_chunk(result, tool_name)
    )


class TestAskUserInvalidArgumentDisplayMask:
    @staticmethod
    def test_ask_user_invalid_options_label_is_masked():
        payload = _parse(
            "[INVALID_ARGUMENT] questions[0].options[0].label is required "
            "and must be a non-empty string.",
            "ask_user",
        )
        assert payload["event_type"] == "chat.tool_result"
        assert payload["tool_name"] == "ask_user"
        assert payload["result"] == "[已跳过]"

    @staticmethod
    def test_ask_user_empty_answers_is_masked():
        payload = _parse(
            "[INVALID_ARGUMENT] answers must include at least one "
            "non-empty response.",
            "ask_user",
        )
        assert payload["result"] == "[已跳过]"

    @staticmethod
    def test_non_ask_user_invalid_argument_is_kept():
        detail = "[INVALID_ARGUMENT] something wrong for bash"
        payload = _parse(detail, "bash")
        assert payload["result"] == detail

    @staticmethod
    def test_ask_user_normal_result_is_kept():
        detail = "提醒间隔改为多少？: 固定每隔1.5小时"
        payload = _parse(detail, "ask_user")
        assert payload["result"] == detail
