# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for reasoning-only empty visible-reply fallback."""

from __future__ import annotations

from unittest.mock import MagicMock

from jiuwenswarm.common.chat_final import (
    fill_reasoning_only_empty_final_content,
    reasoning_only_empty_reply_fallback_text,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_reasoning_only_empty_reply_fallback_text_zh_and_en() -> None:
    zh = reasoning_only_empty_reply_fallback_text("zh")
    en = reasoning_only_empty_reply_fallback_text("en")
    assert zh.startswith("本轮未生成可见回复")
    assert en.startswith("No visible reply was generated this turn")


def test_fill_reasoning_only_empty_final_content_gates() -> None:
    fallback = reasoning_only_empty_reply_fallback_text("zh")

    assert (
        fill_reasoning_only_empty_final_content(
            content="",
            has_visible_streamed_text=False,
            has_reasoning=True,
            lang="zh",
        )
        == fallback
    )
    assert (
        fill_reasoning_only_empty_final_content(
            content="",
            has_visible_streamed_text=True,
            has_reasoning=True,
            lang="zh",
        )
        == ""
    )
    assert (
        fill_reasoning_only_empty_final_content(
            content="",
            has_visible_streamed_text=False,
            has_reasoning=False,
            lang="zh",
        )
        == ""
    )
    assert (
        fill_reasoning_only_empty_final_content(
            content="已添加待办",
            has_visible_streamed_text=False,
            has_reasoning=True,
            lang="zh",
        )
        == "已添加待办"
    )


def test_deep_adapter_apply_preserves_dedicated_fallback() -> None:
    assert (
        JiuWenSwarmDeepAdapter._apply_reasoning_only_empty_reply_fallback(
            has_streamed_content=False,
            had_reasoning_output=True,
            fallback_content="Plan approved.",
            reasoning_only_fallback="FALLBACK",
        )
        == "Plan approved."
    )
    assert (
        JiuWenSwarmDeepAdapter._apply_reasoning_only_empty_reply_fallback(
            has_streamed_content=False,
            had_reasoning_output=True,
            fallback_content="",
            reasoning_only_fallback="FALLBACK",
        )
        == "FALLBACK"
    )
    assert (
        JiuWenSwarmDeepAdapter._apply_reasoning_only_empty_reply_fallback(
            has_streamed_content=True,
            had_reasoning_output=True,
            fallback_content="",
            reasoning_only_fallback="FALLBACK",
        )
        == ""
    )


def test_deep_adapter_fallback_follows_runtime_language() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._resolve_runtime_language = MagicMock(return_value="en")
    assert adapter._reasoning_only_empty_reply_fallback().startswith("No visible reply")
    adapter._resolve_runtime_language.return_value = "zh"
    assert adapter._reasoning_only_empty_reply_fallback().startswith("本轮未生成可见回复")
