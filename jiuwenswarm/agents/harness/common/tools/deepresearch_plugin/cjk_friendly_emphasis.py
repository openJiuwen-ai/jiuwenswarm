# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# This file is a Python port of the attention/strikethrough semantics of:
#   micromark-extension-cjk-friendly and micromark-extension-cjk-friendly-util
#   Copyright (c) Tatsunori Uchino (https://github.com/tats-u)
#   Licensed under the MIT License (https://opensource.org/licenses/MIT)
"""CJK-friendly emphasis and strikethrough flanking for markdown-it-py.

Ports the attention semantics of the relay-claw frontend parser stack
(remark-cjk-friendly + remark-cjk-friendly-gfm-strikethrough, i.e.
micromark-extension-cjk-friendly and its util) onto markdown-it-py by
swapping the emphasis/strikethrough inline rules on one parser instance for
variants whose ``scanDelims`` is the CJK-friendly port below.

Why: the deepresearch rewrite map must classify as rewritable exactly the
paragraphs the frontend selection code does. Both sides decide whether a
delimiter run can open/close emphasis from the characters adjacent to the
run, but they disagree on CJK text: with strict CommonMark a closing ``**``
preceded by CJK punctuation (``**要点。**的``) is not right-flanking, so
markdown-it leaves the run literal and produces nested/mismatched strong
pairs, which makes the inline scanner reject the whole paragraph as
``unsupported_inline`` while the CJK-friendly frontend parses one flat
strong. Selections then fail with UNSUPPORTED_SELECTION.

The delimiter pairing algorithm (``markdown_it.rules_inline.balance_pairs``,
including the rule of three) is untouched — CommonMark attention matching is
identical on both sides. Only the character classification feeding
``can_open``/``can_close`` differs, and that is exactly what ``scanDelims``
computes. Character data comes from ``cjk_unicode_tables``, generated from
the frontend's own classification functions to avoid Unicode-version drift.

Differences from stock markdown-it scanDelims (all inherited from the
micromark forks):

- CJK punctuation does not count as punctuation for flanking, so
  ``**要点。**`` and ``文本**要点**`` parse as strong on both sides.
- A run adjacent to CJK/IVS characters can open/close even next to
  non-CJK punctuation.
- A run whose neighbor is another attention marker (``*``/``_``/``~``)
  always can open/close (micromark behavior; stock markdown-it lacks this).
- ``_``'s intraword restriction consults "space or punctuation", where CJK
  punctuation counts as punctuation.

Known, accepted divergence: the frontend allows single-tilde strikethrough
(``~x~``; singleTilde defaults to true in the micromark fork) while
markdown-it requires ``~~``. A lone ``~`` stays literal text on this side;
that predates the CJK work and only affects rare tilde runs.

Scoping: :func:`apply_cjk_friendly_rules` swaps the rules on one parser
instance only. Every other ``MarkdownIt`` in the process — including
``document_rewrite._INLINE_PARSER`` and any instance a future module or
third-party library creates — keeps stock CommonMark flanking, so the
parsing difference cannot leak across modules. The swapped rules delegate to
the stock ``markdown_it.rules_inline`` tokenize functions with
``scanDelims`` shadowed on that single parse's ``StateInline`` (restored
immediately after the rule returns; no class-level or process-global state
is modified), so no markdown-it-py code is duplicated here and the rules
automatically follow the installed package version.
"""
from __future__ import annotations

from types import MethodType

from markdown_it import MarkdownIt
from markdown_it.rules_inline import emphasis, strikethrough
from markdown_it.rules_inline.state_inline import Scanned, StateInline

from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.cjk_unicode_tables import (
    CJK,
    CJK_AMBIGUOUS_PUNCTUATION_QUOTES,
    IVS,
    NON_EMOJI_GENERAL_USE_VS_RANGE,
    PUNCTUATION,
    WHITESPACE_CHARS,
)

# Character-group bitmasks, mirroring micromark-util-symbol (whitespace=1,
# punctuation=2) and micromark-extension-cjk-friendly-util constantsEx.
_WHITESPACE = 1
_PUNCTUATION = 2
_CJK = 4096
_CJK_PUNCTUATION = _CJK | _PUNCTUATION  # constantsEx.cjkPunctuation
_IVS = 8192
_NON_EMOJI_GENERAL_USE_VS = 16384

# Attention markers after the frontend extensions are combined: core '*'/'_'
# plus '~' from remark-cjk-friendly-gfm-strikethrough.
_ATTENTION_MARKERS = frozenset("*_~")

# U+FE01 — the variation selector that flips curly quotes to CJK punctuation
# (isCjkAmbiguousPunctuation in the util source).
_VARIATION_SELECTOR_1 = 0xFE01

# CJK classification only applies from U+1100 (Hangul Jamo) upward, matching
# the `code >= 4352` guard in the util's classifyCharacter.
_MIN_CJK_CODE_POINT = 4352


def _classify_character(ch: str | None) -> int:
    """Port of the util's classifyCharacter.

    eof (``None``) and JavaScript ``\\s`` characters — which subsume the
    markdown line endings — classify as whitespace. A character can be both
    CJK and punctuation; that combination is the whole point.
    """
    if ch is None or ch in WHITESPACE_CHARS:
        return _WHITESPACE
    code_point = ord(ch)
    value = 0
    if code_point >= _MIN_CJK_CODE_POINT:
        low, high = NON_EMOJI_GENERAL_USE_VS_RANGE
        if low <= code_point <= high:
            return _NON_EMOJI_GENERAL_USE_VS
        if code_point in IVS:
            return _IVS
        if code_point in CJK:
            value |= _CJK
    if code_point in PUNCTUATION:
        value |= _PUNCTUATION
    return value


def _classify_preceding_character(
    before: int, previous: str, two_previous: str | None
) -> int:
    """Port of the util's classifyPrecedingCharacter.

    When the character before the run is a non-emoji general-use variation
    selector (U+FE00..U+FE0E), its effective group is resolved from the
    character before it. ``two_previous`` is ``None`` at line starts, matching
    the chunked-buffer boundary in the micromark forks.
    """
    if before != _NON_EMOJI_GENERAL_USE_VS:
        return before
    two_before = _classify_character(two_previous)
    if not two_previous or two_before & _WHITESPACE:
        return before
    if (
        ord(previous) == _VARIATION_SELECTOR_1
        and ord(two_previous) in CJK_AMBIGUOUS_PUNCTUATION_QUOTES
    ):
        return _CJK_PUNCTUATION
    return two_before & ~_IVS


def _is_unicode_whitespace(category: int) -> bool:
    return bool(category & _WHITESPACE)


def _is_non_cjk_punctuation(category: int) -> bool:
    # Punctuation bit set without the CJK bit: exactly the non-CJK subset.
    return (category & _CJK_PUNCTUATION) == _PUNCTUATION


def _is_cjk(category: int) -> bool:
    return bool(category & _CJK)


def _is_cjk_or_ivs(category: int) -> bool:
    return bool(category & (_CJK | _IVS))


def _is_space_or_punctuation(category: int) -> bool:
    # Used by the '_' intraword rule; CJK punctuation keeps its punct bit here.
    return bool(category & (_WHITESPACE | _PUNCTUATION))


def _cjk_scan_delims(state: StateInline, start: int, can_split_word: bool) -> Scanned:
    """CJK-friendly port of ``StateInline.scanDelims`` for attention markers.

    Callers (the two rules below) guarantee ``state.src[start]`` is one of
    ``*``/``_``/``~``. ``can_split_word`` is kept for signature compatibility
    with stock ``scanDelims`` (markdown-it-py names it ``canSplitWord``; both
    rules pass it positionally); the branches below key off the marker
    character, mirroring how the micromark forks define separate attention
    ('*'/'_') and strikethrough ('~') tokenizers.
    """
    marker = state.src[start]

    pos = start
    maximum = state.posMax
    while pos < maximum and state.src[pos] == marker:
        pos += 1
    count = pos - start

    # Neighbors as full code points: Python strings are scalar values, so the
    # UTF-16 surrogate gymnastics of the micromark forks is unnecessary.
    previous = state.src[start - 1] if start > 0 else None
    after = state.src[pos] if pos < maximum else None
    two_previous = (
        state.src[start - 2] if start >= 2 and state.src[start - 2] != "\n" else None
    )

    before_raw = _classify_character(previous)
    before_primary = _classify_preceding_character(before_raw, previous, two_previous)
    after_category = _classify_character(after)

    before_non_cjk_punct = _is_non_cjk_punctuation(before_primary)
    before_space_or_non_cjk_punct = before_non_cjk_punct or _is_unicode_whitespace(
        before_primary
    )
    after_non_cjk_punct = _is_non_cjk_punctuation(after_category)
    after_space_or_non_cjk_punct = after_non_cjk_punct or _is_unicode_whitespace(
        after_category
    )

    if marker == "~":
        # Strikethrough fork: same flanking idea, no attention-marker clause,
        # and the closing punct test reads the raw (not resolved) before-group.
        before_cjk_or_ivs = _is_cjk(before_primary) or before_raw == _IVS
        can_open = not after_space_or_non_cjk_punct or (
            after_category == _PUNCTUATION
            and (before_space_or_non_cjk_punct or before_cjk_or_ivs)
        )
        can_close = not before_space_or_non_cjk_punct or (
            before_raw == _PUNCTUATION
            and (after_space_or_non_cjk_punct or _is_cjk(after_category))
        )
        return Scanned(can_open, can_close, count)

    # Attention fork ('*' and '_').
    before_cjk_or_ivs = _is_cjk_or_ivs(before_primary)
    open_ = (
        not after_space_or_non_cjk_punct
        or (
            after_non_cjk_punct
            and (before_space_or_non_cjk_punct or before_cjk_or_ivs)
        )
        or after in _ATTENTION_MARKERS
    )
    close_ = (
        not before_space_or_non_cjk_punct
        or (
            before_non_cjk_punct
            and (after_space_or_non_cjk_punct or _is_cjk(after_category))
        )
        or previous in _ATTENTION_MARKERS
    )
    if marker == "*":
        return Scanned(open_, close_, count)
    # '_' intraword rule (emphasis.tokenize passes can_split_word=False for '_').
    can_open = open_ and (_is_space_or_punctuation(before_primary) or not close_)
    can_close = close_ and (_is_space_or_punctuation(after_category) or not open_)
    return Scanned(can_open, can_close, count)


def _cjk_emphasis_tokenize(state: StateInline, silent: bool) -> bool:
    """Stock ``emphasis.tokenize`` with this parse's ``scanDelims`` CJK-friendly.

    Shadows ``scanDelims`` on the state object for the duration of the stock
    rule only — per-parse and per-thread; the class and the process are
    never modified — so the markdown-it-py rule body is reused as installed
    instead of being copied into this repository.
    """
    state.scanDelims = MethodType(_cjk_scan_delims, state)
    try:
        return emphasis.tokenize(state, silent)
    finally:
        del state.scanDelims


def _cjk_strikethrough_tokenize(state: StateInline, silent: bool) -> bool:
    """Stock ``strikethrough.tokenize`` with this parse's ``scanDelims`` CJK-friendly.

    Same per-parse shadowing as :func:`_cjk_emphasis_tokenize`.
    """
    state.scanDelims = MethodType(_cjk_scan_delims, state)
    try:
        return strikethrough.tokenize(state, silent)
    finally:
        del state.scanDelims


def apply_cjk_friendly_rules(parser: MarkdownIt) -> MarkdownIt:
    """Swap emphasis/strikethrough rules on ``parser`` for CJK-friendly ones.

    Instance-scoped: only ``parser`` changes behavior; every other
    ``MarkdownIt`` instance in the process keeps stock CommonMark flanking.
    """
    parser.inline.ruler.at("emphasis", _cjk_emphasis_tokenize)
    parser.inline.ruler.at("strikethrough", _cjk_strikethrough_tokenize)
    return parser
