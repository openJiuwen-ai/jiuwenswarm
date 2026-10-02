"""The deployment-level working-style file (workmode).

One markdown file per deployment answers "how does this colleague work" -- tone,
verbosity, the rhythm of handling comment batches. It is the **style half** of §4.8
only: authority never lives here. A clause like "you may edit without approval" is
inert text -- the permission table, the unattended closed set and the range rail are
code and do not read this file.

Layering (§4.8.2): the safety contract (code) comes first, this file second, the
per-document conventions comment third. Conventions may override style clauses here;
nothing here or there can widen authority.

The file is **re-read on every dispatch**: editing it never fires a config event, so
caching would mean the change takes effect at the next restart and nobody knows why.
Missing file falls back to the built-in template silently (that is the normal state
of a fresh deployment); an unreadable or oversized file falls back too but is worth
one warning, which the caller emits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

WORKMODE_MAX_BYTES = 16 * 1024

_BUILTIN_ZH = """\
# Co-scribe 工作方式

这份文件描述这位 agent 同事怎么做事，只承载风格与节奏。授权类规则（能否改正文、
是否需要人批准、可操作范围）不在这里——写了也不生效，那些由代码强制。

## 回复风格
- 用评论者的语言回复；修改内容本身跟随文档正文的语言。
- 回复简短、对事：先给结论，再给一句理由，不复述对方的话。
- 拿不准对方要什么时，在线程里问清，不猜。

## 修改习惯
- 编辑最小化：只改被要求的那处，不顺手优化。
- 是非类提问只回答；只有发现确实的错误才附带提议。

## 批注攒批（一次处理多条批注时）
- 逐条处理：每条批注对应各自的修改，不同批注的诉求不合并改写。
- 同一处文字被多条批注涉及时，合并为一次修改，在相关线程分别回执说明。
- 每条线程留一句回执：改了什么、为什么；问题类批注在线程里作答。
- 全部处理完后在对话里汇总：处理了几条、改了几处、有无跳过及原因。
"""

_BUILTIN_EN = """\
# Co-scribe working style

This file describes how this agent colleague works. It carries style and rhythm
only. Authority rules (whether the body may be edited, whether a human must approve,
what ranges may be touched) do not live here -- writing them here does nothing;
those are enforced in code.

## Replying
- Answer in the commenter's language; edits themselves follow the document's language.
- Keep replies short and on point: conclusion first, one reason, no echoing.
- When the request is unclear, ask in the thread instead of guessing.

## Editing habits
- Minimal edits: change exactly what was asked, no drive-by polishing.
- Yes/no questions get answers only; propose a fix only for a genuine error.

## Batch review (handling several comments at once)
- One comment, one edit: requests from different comments are not merged into one
  rewrite.
- When several comments touch the same passage, make one edit and leave a receipt in
  each related thread.
- Every thread gets one receipt: what changed and why; question-type comments get
  their answer in the thread.
- Finish with a summary in chat: how many comments handled, how many edits, anything
  skipped and why.
"""


@dataclass(frozen=True)
class Workmode:
    text: str
    source: str  # "file" | "builtin"
    truncated: bool = False
    error: str | None = None


def prefer_zh_from_words(words: Iterable[str] | str | None) -> bool:
    """Pick the built-in template's language from a configured phrase.

    The deployment already states its language in the conventions marker (the
    default is ``co-scribe 约定``; an English deployment writes an English one), so
    no separate language key is needed. Import is lazy for the same reason as
    elsewhere in this package: texts/turn_prompt import each other's neighbours.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import looks_chinese

    if words is None:
        return True
    sample = words if isinstance(words, str) else "".join(str(w) for w in words)
    return looks_chinese(sample) if sample else True


def builtin_template(*, prefer_zh: bool = True) -> str:
    return _BUILTIN_ZH if prefer_zh else _BUILTIN_EN


def resolve_workmode_path(configured: str | None) -> Path:
    """The one place the file's location is decided.

    The chat tools and the watcher must resolve identically, or an edit made in chat
    lands in a file the watcher never reads. Empty config means the default location
    under the workspace config directory -- the same directory that already holds
    config.yaml and clouddoc-keys/.
    """
    if configured and str(configured).strip():
        return Path(str(configured).strip()).expanduser()
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.deployment import (
        workspace_dir as get_user_workspace_dir,
    )

    return get_user_workspace_dir() / "config" / "clouddoc-workmode.md"


def _truncate_to_bytes(text: str, limit: int) -> tuple[str, bool]:
    if len(text.encode("utf-8")) <= limit:
        return text, False
    out: list[str] = []
    size = 0
    for ln in text.split("\n"):
        b = len(ln.encode("utf-8")) + 1
        if size + b > limit:
            break
        out.append(ln)
        size += b
    if not out:
        # A single line larger than the whole budget: cut by characters, or the
        # boundary rule would silently drop the entire file.
        clipped = text
        while clipped and len(clipped.encode("utf-8")) > limit:
            clipped = clipped[:-64] if len(clipped) > 64 else ""
        return clipped, True
    return "\n".join(out), True


def load_workmode(configured_path: str | None, *, prefer_zh: bool = True) -> Workmode:
    """Read the working-style text for one dispatch.

    Missing file -> builtin, no error (normal state). Unreadable -> builtin with the
    error recorded. Oversized -> file text truncated on a line boundary, flagged.
    An existing but empty file is respected as-is: a deployer who emptied the file
    asked for no style text, not for the template back.
    """
    path = resolve_workmode_path(configured_path)
    if not path.is_file():
        return Workmode(text=builtin_template(prefer_zh=prefer_zh), source="builtin")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Workmode(
            text=builtin_template(prefer_zh=prefer_zh), source="builtin", error=str(exc)
        )
    text, truncated = _truncate_to_bytes(raw, WORKMODE_MAX_BYTES)
    return Workmode(text=text, source="file", truncated=truncated)


def edit_workmode(
    configured_path: str | None,
    old_string: str,
    new_string: str,
    *,
    prefer_zh: bool = True,
) -> dict:
    """Apply one unique-match replacement to the working-style file.

    The file is materialized from the built-in template on first edit, so that what
    workmode_get displayed is exactly what old_string is matched against -- editing
    a phantom file the user never saw would make every first edit fail with "not
    found". Zero matches and multiple matches both refuse: applying at "the first"
    of two matches would be a guess, and this file is read on every dispatch.
    """
    if not old_string:
        return {"ok": False, "detail": "old_string 不能为空：先用 clouddoc_workmode_get 查看当前内容再修改。"}
    path = resolve_workmode_path(configured_path)
    try:
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(builtin_template(prefer_zh=prefer_zh), encoding="utf-8")
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "detail": f"工作方式文件不可读写：{exc}"}
    hits = text.count(old_string)
    if hits == 0:
        return {"ok": False, "detail": "old_string 在工作方式文件中找不到；先 clouddoc_workmode_get 取当前原文。"}
    if hits > 1:
        return {"ok": False, "detail": f"old_string 命中 {hits} 处，无法确定改哪一处；请扩大 old_string 使其唯一。"}
    updated = text.replace(old_string, new_string, 1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return {"ok": False, "detail": f"工作方式文件写入失败：{exc}"}
    size = len(updated.encode("utf-8"))
    detail = "已修改，下一次派发即生效。"
    if size > WORKMODE_MAX_BYTES:
        detail += f" 注意：文件已达 {size} 字节，超出 {WORKMODE_MAX_BYTES} 的部分在注入时会被按行截断。"
    return {"ok": True, "detail": detail, "path": str(path), "size": size}
