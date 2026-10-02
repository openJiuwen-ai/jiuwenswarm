"""D19 tier 3a: the predicate rail.

The mandate machinery verifies the boundary of an action, never its result: an
instruction saying "剪短" (shorten) is satisfied by machinery-standards the moment
the edit lands inside the quoted range -- even if the rewrite came out *longer*.
The full result belongs to ring ⑥, a person; but a family of explicit instructions
implies a predicate that is mechanically decidable over the edit pair itself, and
for those the data is already in hand before the write: old and new are both known.

So this rail is a pure function over the edits, evaluated **before** the write, in
the same posture as the range rail: the range rail bounds *where*, this bounds
*which direction*. A definite violation refuses the whole batch; anything the
lexicon does not confidently recognise passes untouched -- a predicate rail that
guesses would manufacture refusals out of its own misreadings.

The honest boundary, stated once and pinned by tests: this verifies the
**mechanizable projection** of an instruction, not the instruction. "剪短但保留三个
要点" has a checkable half (length) and a semantic half (the three points), and the
second half stays with the person. Claiming more than the projection would be the
capability-facade failure in reverse -- delivering a sliver and declaring the whole.
"""

from __future__ import annotations

import re
from typing import Callable

# An edit pair as the rail sees it: (old, new). ``old`` is None on the region form,
# where the before-image is computed by the provider and not available here; the
# predicates that need it skip rather than guess.
Pair = tuple[str | None, str]

_QUOTE = r"[「\"“'『]([^」\"”'』]{1,120})[」\"”'』]"


def _shorter(pairs: list[Pair]) -> str | None:
    olds = [o for o, _ in pairs]
    if any(o is None for o in olds):
        return None  # region form carries no before-image here; nothing to compare
    total_old = sum(len(o) for o in olds if o is not None)
    total_new = sum(len(n) for _, n in pairs)
    if total_new >= total_old:
        return f"结果 {total_new} 字符，不短于原文 {total_old} 字符"
    return None


def _emptied(pairs: list[Pair]) -> str | None:
    kept = [n for _, n in pairs if n.strip()]
    if kept:
        return f"仍写入了非空内容（{len(kept)} 处）"
    return None


def _removed(target: str) -> Callable[[list[Pair]], str | None]:
    def check(pairs: list[Pair]) -> str | None:
        if any(target in n for _, n in pairs):
            return f"「{target}」仍出现在写入内容中"
        return None

    return check


def _became(target: str) -> Callable[[list[Pair]], str | None]:
    def check(pairs: list[Pair]) -> str | None:
        if not any(target in n for _, n in pairs):
            return f"写入内容中找不到「{target}」"
        return None

    return check


def implied_predicates(instruction: str) -> list[tuple[str, Callable[[list[Pair]], str | None]]]:
    """The predicates an instruction confidently implies, as (label, check) pairs.

    Conservative on purpose: a verb the lexicon does not match yields nothing, and
    an instruction matching several verbs yields all of them -- "删掉X，其余剪短"
    should be held to both.
    """
    text = instruction or ""
    out: list[tuple[str, Callable[[list[Pair]], str | None]]] = []
    if re.search(r"剪短|缩短|精简|shorten|make it shorter", text):
        out.append(("剪短：结果应比原文短", _shorter))
    if re.search(r"清空", text):
        out.append(("清空：结果应为空", _emptied))
    for m in re.finditer(r"(?:删掉|删除|去掉)\s*" + _QUOTE, text):
        t = m.group(1)
        out.append((f"删掉「{t}」：结果不应再含它", _removed(t)))
    for m in re.finditer(r"(?:改成|换成|替换[为成]?)\s*" + _QUOTE, text):
        t = m.group(1)
        out.append((f"改成「{t}」：结果应含它", _became(t)))
    return out


def check_result_predicates(instruction: str, pairs: list[Pair]) -> str | None:
    """None when every implied predicate holds (or none is implied); otherwise the
    first violation, worded for the refusal that carries it."""
    for label, check in implied_predicates(instruction):
        detail = check(pairs)
        if detail:
            return f"指令蕴含的结果条件不满足——{label}；实际：{detail}"
    return None
