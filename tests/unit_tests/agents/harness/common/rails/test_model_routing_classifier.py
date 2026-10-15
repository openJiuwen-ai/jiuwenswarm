# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the auto-gear task classifier (``rails/model_routing/classifier.py``).

The classifier is now a self-contained Python module — score table, rules and the
TF-IDF corpus are hardcoded (no runtime config loading). These tests pin the contract
the rail depends on:

    score = SCORE_TABLE[(category, difficulty)]   (difficulty fallback 25/40/60)
    continuation utterances -> (CONTINUATION_SCORE, CONTINUATION_CATEGORY, "skip")

The prompt -> (score, category, difficulty) rows below are recorded from the previous
JSON-driven implementation, so they double as an equivalence regression on the port.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import (
    CATEGORIES,
    CONTINUATION_CATEGORY,
    CONTINUATION_SCORE,
    DEFAULT_SCORE_BY_DIFFICULTY,
    DEFAULT_SCORE_FALLBACK,
    DIFFICULTIES,
    SCORE_TABLE,
    classify,
    validate_score,
)


def test_score_table_is_the_expected_15_entries():
    assert SCORE_TABLE == {
        ("chat", "easy"): 5,
        ("chat", "medium"): 20,
        ("chat", "hard"): 40,
        ("reasoning", "easy"): 20,
        ("reasoning", "medium"): 40,
        ("reasoning", "hard"): 60,
        ("coding", "easy"): 25,
        ("coding", "medium"): 45,
        ("coding", "hard"): 65,
        ("summarization", "easy"): 15,
        ("summarization", "medium"): 25,
        ("summarization", "hard"): 45,
        ("format", "easy"): 5,
        ("format", "medium"): 10,
        ("format", "hard"): 30,
    }
    assert CATEGORIES == ("chat", "reasoning", "coding", "summarization", "format")
    assert DIFFICULTIES == ("easy", "medium", "hard")
    assert DEFAULT_SCORE_BY_DIFFICULTY == {"easy": 25, "medium": 40, "hard": 60}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("", (5, "chat", "easy")),
        ("   ", (5, "chat", "easy")),
        ("你好", (5, "chat", "easy")),
        ("hello", (5, "chat", "easy")),
        ("帮我写一个快速排序函数", (25, "coding", "easy")),
        ("debug this code", (65, "coding", "hard")),
        ("请总结这篇文章的要点", (15, "summarization", "easy")),
        ("请逐步推导这个数学证明", (20, "reasoning", "easy")),
        ("1+1", (20, "reasoning", "easy")),
        ("把这段 JSON 转成 CSV", (5, "format", "easy")),
        # 长输入触发长度信号 → 难度抬到 medium（无规则命中时按 chat 兜底）
        ("x" * 400, (20, "chat", "medium")),
    ],
)
async def test_classify_known_prompts(prompt, expected):
    assert await classify(prompt) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    ["继续", "好的", "ok", "continue", "请继续", "please continue", "好继续", "好的请继续"],
)
async def test_continuation_utterances_return_sentinel(prompt):
    """续写/确认指令短路：哨兵让 rail 跳过重路由（否则 -1 会被 validate_score 兜成 60）。"""
    assert await classify(prompt) == (CONTINUATION_SCORE, CONTINUATION_CATEGORY, "skip")


@pytest.mark.asyncio
async def test_classify_only_returns_known_categories_and_difficulties():
    for prompt in ("你好", "debug this code", "把这段 JSON 转成 CSV", "请总结要点"):
        score, category, difficulty = await classify(prompt)
        assert category in (*CATEGORIES, CONTINUATION_CATEGORY)
        assert difficulty in (*DIFFICULTIES, "skip")
        assert score == SCORE_TABLE.get((category, difficulty), score)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0, 0),
        (50, 50),
        (100, 100),
        (-1, DEFAULT_SCORE_FALLBACK),  # 越界（continuation 哨兵若漏到此处）→ 兜底
        (101, DEFAULT_SCORE_FALLBACK),
        ("80", 80),
        ("abc", DEFAULT_SCORE_FALLBACK),
        (None, DEFAULT_SCORE_FALLBACK),
    ],
)
def test_validate_score(raw, expected):
    assert validate_score(raw) == expected


# ---- CJK 词边界回归：`\b` 与 CJK 相邻时永不匹配 ---- #


@pytest.mark.parametrize(
    "prompt",
    [
        "很复杂的系统怎么设计",
        "分析架构问题",
        "架构分析",
        "请说明一下",
    ],
)
def test_ascii_word_boundaries_make_cjk_keywords_match(prompt):
    r"""CJK 关键词在**紧邻**其他 CJK 字时也要命中。

    旧行为：规则用 ``\b(complex|...|复杂|架构)\b``，而 Python 的 ``\b`` 以 ``\w``
    定义、Unicode 下 ``\w`` 把 CJK 也算词字符 —— 「很复杂的系统」里 很/复 都是词字符、
    其间无边界，规则永不命中（只有写成「很 复杂 的系统」加空格才行）。
    """
    from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import _rules

    for pattern, *_ in _rules():
        if pattern.search(prompt):
            return
    pytest.fail(f"no rule matched {prompt!r} — CJK 词边界回归")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt, expected_category, expected_difficulty",
    [
        ("很复杂的系统怎么设计", "chat", "hard"),
        ("请分析架构问题", "reasoning", "hard"),
        ("架构分析", "reasoning", "hard"),
    ],
)
async def test_cjk_hard_prompts_reach_hard_difficulty(prompt, expected_category, expected_difficulty):
    """中文 hard 提示词能拿到 hard 档（修复前封顶 easy/medium，永远到不了切换线）。"""
    score, category, difficulty = await classify(prompt)
    assert (category, difficulty) == (expected_category, expected_difficulty)
    assert score == SCORE_TABLE[(expected_category, expected_difficulty)]
    assert score >= 40  # 至少达到 rail 的切换阈值


@pytest.mark.parametrize(
    "prompt, should_match",
    [
        ("a complex system", True),
        ("a complicated design", True),
        ("complexity", False),  # ASCII 词内不得误命中（与 \b 语义等价）
        ("hint", False),  # `hi` 前缀规则同理
    ],
)
def test_ascii_boundaries_keep_english_semantics(prompt, should_match):
    """英文语义与 ``\b`` 等价：不得在更大的 ASCII 词内部命中。"""
    from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import _ascii_word_boundaries
    import re

    pat = re.compile(_ascii_word_boundaries(r"(?i)\b(complex|complicated|advanced|复杂|架构)\b"))
    assert (pat.search(prompt) is not None) is should_match

