"""Regression tests for the Output Language rules.

Pins that reply language follows ``preferred_response_language`` when it is
``en`` / ``zh``, and matches this turn's ``content`` when the field is
``match`` or omitted — not the UI ``preferred_language``.

Also pins that visible thinking / reasoning narration follows the same
output-language section, and the normalization behaviour of the section
builders so that raw settings such as ``"zh"`` / ``"EN"`` cannot silently
fall into the English branch.
"""

import pytest

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    _input_prompt,
    _output_prompt,
)

OLD_CN_FALLBACK = "当前消息未提供该字段时，默认使用简体中文。"
OLD_EN_FALLBACK = "If the current message omits this field, default to English."
NEW_CN_MATCH = (
    "`match` 或未提供该字段时与本轮消息 `content` 同语"
)
NEW_EN_MATCH = (
    "`match` or a missing field for the same language as this turn's `content`"
)
CN_THINKING_RULE = (
    "可见的思考过程与推理叙述也遵循本小节，与最终回复同语；不得因模型习惯改用语种。"
)
EN_THINKING_RULE = (
    "Visible thinking and reasoning narration follow this section and match "
    "the final reply language; do not switch language out of model habit."
)


def _text(section) -> str:
    """Return the rendered single-language body of a PromptSection."""
    return next(iter(section.content.values()))


def test_zh_output_rules_match_content_when_not_pinned():
    text = _text(_output_prompt("cn"))

    assert "`preferred_response_language`" in text
    assert "`en` 用英文" in text
    assert "`zh` 用简体中文" in text
    assert NEW_CN_MATCH in text
    assert OLD_CN_FALLBACK not in text
    assert CN_THINKING_RULE in text
    assert "## 思考 / 推理语言" not in text
    assert "`reasoning_content`" not in text
    assert "不得默认使用英文思考" not in text
    assert text.count(NEW_CN_MATCH) == 1


def test_en_output_rules_match_content_when_not_pinned():
    text = _text(_output_prompt("en"))

    assert "`preferred_response_language`" in text
    assert "`en` for English" in text
    assert "`zh` for Simplified Chinese" in text
    assert NEW_EN_MATCH in text
    assert OLD_EN_FALLBACK not in text
    assert EN_THINKING_RULE in text
    assert "## Reasoning / Thinking Language" not in text
    assert "`reasoning_content`" not in text
    assert "Do not default to English thinking" not in text
    assert text.count(NEW_EN_MATCH) == 1


def test_zh_input_docs_list_match_value():
    text = _text(_input_prompt("cn"))

    assert "en、zh 或 match" in text


def test_en_input_docs_list_match_value():
    text = _text(_input_prompt("en"))

    assert "en, zh, or match" in text


@pytest.mark.parametrize(("language", "expected_key"), [("cn", "cn"), ("en", "en")])
def test_section_carries_exactly_one_language_key(language, expected_key):
    """The builder only ever ships one body, so the rendered key must match.

    ``PromptSection.render`` falls back to ``DEFAULT_LANGUAGE``; a mismatch
    between the branch that produced the text and the key stored would render
    the wrong language without raising.
    """

    for section in (_input_prompt(language), _output_prompt(language)):
        assert list(section.content) == [expected_key]


@pytest.mark.parametrize("language", ["zh", "ZH", "EN", "zh-CN", "en-US", "", None])
def test_section_builders_normalize_language_before_branching(language):
    """Anything that is not ``en`` must resolve to the Chinese body.

    Before normalization ``"zh"`` / ``"EN"`` fell through to the English branch
    because the builders compare against the literal ``"cn"``.
    """

    assert "输出规则" in _text(_output_prompt(language))
    assert "输入说明" in _text(_input_prompt(language))


def test_english_still_resolves_to_english_after_normalization():
    assert "# Output Rules" in _text(_output_prompt("en"))
    assert "# Input Instructions" in _text(_input_prompt("en"))
