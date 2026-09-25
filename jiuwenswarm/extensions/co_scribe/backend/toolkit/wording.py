"""The toolkit's own user-facing wording, and the language heuristic that picks it.

Relocated here from the gateway during the loosening cuts (§25.5): these strings
are spoken by the write tools themselves -- the receipt reply a collaborator reads
in a thread -- so they belong to the toolkit, and the gateway's message table
composes from this one rather than keeping a second copy.
"""

from __future__ import annotations

import unicodedata


def looks_chinese(text: str) -> bool:
    """Whether a text is written in Chinese. Used to pick wording, never as a
    test: guessing wrong only means the message appears in the other language."""
    return any("一" <= ch <= "鿿" for ch in text or "")


# (zh, en) pairs for everything the toolkit itself says into a document.
TOOL_TEXTS: dict[str, tuple[str, str]] = {
    "applied_highlighted": ("已直接修改并以黄色高亮标出。",
                            "Edited directly; the changes are highlighted in yellow."),
    "applied_listed":     ("已直接修改。本平台的写入通道不支持高亮，改动逐条列在下面：\n{listed}",
                           "Edited directly. This platform's write channel cannot highlight, "
                           "so the changes are listed below:\n{listed}"),
    "edit_line":          ("- 「{old}」→「{new}」", "- \"{old}\" -> \"{new}\""),
    "edit_line_deleted":  ("- 删除「{old}」", "- removed \"{old}\""),
    # An edit that only adds text renders as "X → X…" when the pair is printed whole,
    # because the new text *starts with* the old one. On a platform with no highlight
    # this line is the only place the reader learns what changed, so an append says so.
    "edit_line_added":    ("- 在「{where}」之后加入 {n} 字：「{added}」",
                           "- after \"{where}\", added {n} characters: \"{added}\""),
    # Progress, written over the watcher's placeholder while the turn runs (D-prog).
    # An unattended turn is three to four sequential model calls -- measured at 45 to
    # 52 seconds against a live document -- and until these existed the thread showed
    # "Working on it…" for all of it, which reads as a hang rather than as work. They
    # are narration only: a failure to write one is swallowed, because a note about
    # the work must never be able to cost the work.
    "progress_reading":   ("⏳ 正在读取文档…", "⏳ Reading the document…"),
    "progress_applying":  ("⏳ 正在核对批注选区并写入…", "⏳ Checking the comment's range and writing…"),
    "progress_replying":  ("⏳ 正在整理回复…", "⏳ Composing the reply…"),
    "revert_hint":        ("不满意可 @ 我说明；要撤销这次改动，请用平台的版本历史（Google「版本记录」/飞书「历史版本」）。",
                           "If this is not what you wanted, @-mention me with what to "
                           "change; to undo it, use the platform's version history."),
}


def tool_msg(key: str, lang_sample: str = "", **kw: object) -> str:
    """Pick a language from ``lang_sample`` and format. Same contract as the
    gateway's ``msg``; only the table is the toolkit's own."""
    zh, en = TOOL_TEXTS[key]
    return (zh if looks_chinese(lang_sample) else en).format(**kw)


def normalize(s: str) -> str:
    """Normalize before comparing keywords: NFKC, strip zero-width marks, trim, casefold.

    **The single implementation.** This decides whether a reply counts as an approval,
    so a second copy is a live hazard -- fix one, miss the other, and the two paths
    disagree about what "approve" means with nothing failing loudly. There were three
    copies; two are gone.

    NBSP needs no separate step: NFKC already maps U+00A0 to a plain space. An explicit
    replace used to sit here and, since both of its arguments were ordinary spaces, it
    had never done anything -- while the comment above it claimed NBSP handling that
    NFKC was in fact providing.
    """
    s = unicodedata.normalize("NFKC", s or "")
    for ch in ("​", "‌", "‍", "﻿"):
        s = s.replace(ch, "")
    return s.strip().casefold()
