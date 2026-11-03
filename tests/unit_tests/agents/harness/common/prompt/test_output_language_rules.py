"""Regression tests for the Output Language rules (PR #109).

Covers the two prompt changes introduced by
``fix(prompt): honor preferred response language``:

1. zh section: a message's ``preferred_response_language`` wins, and the
   fallback stays Simplified Chinese;
2. en section: the same rule applies, and the fallback becomes English
   (it used to be Simplified Chinese).

Also pins the normalization behaviour of the section builders so that raw
settings such as ``"zh"`` / ``"EN"`` cannot silently fall into the English
branch.
"""

import pytest

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    _input_prompt,
    _output_prompt,
)

# --- texts that the two hunks must produce / must not produce -----------------

OLD_CN_FALLBACK = "用户未指定时，默认使用简体中文。"
NEW_CN_FALLBACK = "当前消息未提供该字段时，默认使用简体中文。"
OLD_EN_FALLBACK = "If the user does not specify one, default to Simplified Chinese."
NEW_EN_FALLBACK = "If the current message omits this field, default to English."


def _text(section) -> str:
    """Return the rendered single-language body of a PromptSection."""
    return next(iter(section.content.values()))


def test_zh_output_rules_follow_preferred_response_language():
    text = _text(_output_prompt("cn"))

    assert "`preferred_response_language`" in text
    assert "`en` 为英文" in text
    assert "`zh` 为简体中文" in text
    assert NEW_CN_FALLBACK in text
    assert OLD_CN_FALLBACK not in text


def test_en_output_rules_follow_preferred_response_language():
    text = _text(_output_prompt("en"))

    assert "`preferred_response_language`" in text
    assert "`en` for English" in text
    assert "`zh` for Simplified Chinese" in text
    assert NEW_EN_FALLBACK in text
    assert OLD_EN_FALLBACK not in text


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
