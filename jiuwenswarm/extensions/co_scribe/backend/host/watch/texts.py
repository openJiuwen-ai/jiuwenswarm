"""Every string the watcher writes into a document, in both languages.

The language policy has two layers, and a third entry that is history:

* **System text and the agent's own prose** -- follows the language of the
  triggering comment. The reader is the person in the document, and a
  deployment-level switch would have to pick a side in a document where Chinese
  and English collaborators share a thread.
* **The write tools' own five sentences** -- composed in from the toolkit's
  wording module, so a receipt reply reads the same whether the write came from
  chat or from a comment.
* **Retired notices** -- kept in the table read-only, because the watcher
  recognises its own earlier posts by exact text.
"""

from __future__ import annotations

from jiuwenswarm.extensions.co_scribe.backend.host.watch.turn_prompt import looks_chinese

from jiuwenswarm.extensions.co_scribe.backend.toolkit.wording import (  # noqa: E402
    TOOL_TEXTS as _TOOL_TEXTS,
)

_TEXTS: dict[str, tuple[str, str]] = {
    # The five entries the write tools speak themselves live in the toolkit's
    # wording module (single source); this table composes them back in.
    **_TOOL_TEXTS,
    # key: (Chinese, English)
    "placeholder":        ("⏳ 正在处理…", "⏳ Working on it…"),
    # A bare reply no longer continues a thread: participation needs a mention, so
    # that a discussion between other collaborators does not wake the agent. Every
    # text that asks for something back therefore asks to be named.
    "turn_incomplete":    ("这一轮没有完成。在本线程 @ 我一句（如「@我 继续」）即可让我重试。（编号 {ref}）",
                           "This turn didn't complete. @-mention me in this thread "
                           "(e.g. \"@me continue\") and I'll retry. (ref {ref})"),
    # D2②: the one thing said to a collaborator when no turn will run. With the
    # binary it states the **fact** -- this document's watch is off -- rather than
    # narrating an authority ladder the reader cannot see. "Not yet authorized"
    # also read as a temporary condition about to resolve itself; off is a
    # setting, and the sentence now says who changes it and where.
    "watch_unauthorized": ("已记录。这篇文档的值守没有开启，我不会自动处理这条批注。"
                           "部署主人可以在 Jiuwen 的「Docs」面板里为这篇文档开启值守；"
                           "开启后请在本线程再 @ 我一次。",
                           "Recorded. The watch is off for this document, so I will "
                           "not handle this comment automatically. The deployment "
                           "owner can turn it on for this document in Jiuwen's Docs "
                           "panel; once it is on, @ me again in this thread."),
    # Retired: **nothing sends this any more**. Suspension left the state space
    # (a watch is off or on), so no gate verdict maps to it. The entry stays
    # because the watcher's own-notice dedup identifies a reply by its exact
    # text: a thread that already holds this line from an earlier release must
    # still be recognised as the watcher's, or the sweep starts treating it as a
    # person's words. Read-only history, never an outgoing message.
    "watch_suspended":    ("已记录，本轮暂不安排处理。",
                           "Recorded; not scheduled this round."),
    # D9 / review M-cluster-1: the third visible wording -- over budget is queued
    # work, and silence here would be indistinguishable from silent dropping.
    "watch_over_budget":  ("已记录并排队：今日的自动处理额度已用完。在本线程重新 @ 我即视为新任务。",
                           "Recorded and queued: today's automatic-handling budget is "
                           "used up. @-ing me again in this thread counts as a fresh task."),
    # The wording above before assignment retired. Read-only history, like
    # ``watch_suspended``: a thread still holding it must read as this watcher's
    # own notice, never as a person's words.
    "watch_over_budget_legacy": (
        "已记录并排队：今日的自动处理额度已用完。重新指派（或在本线程重新 @ 我）即视为新任务。",
        "Recorded and queued: today's automatic-handling budget is used up. "
        "Re-assigning (or @-ing me again in this thread) counts as a fresh task."),
    "ack_conventions":    ("已读取本文档约定（{count} 条）。",
                           "Conventions noted ({count} item(s))."),
    "ack_truncated":      ("已读取本文档约定（{count} 条），超出 {limit} 字符的部分已按行截断。",
                           "Conventions noted ({count} item(s)); text beyond {limit} characters was truncated by line."),
    # The receipt apply_for_comment posts under the comment it answered (D1d). These
    # used to be Chinese literals in the tool, so an English thread got a Chinese
    # receipt on every direct edit -- the one reply the reader sees on that turn.
}


def msg(key: str, lang_sample: str = "", **kw: object) -> str:
    """Pick a language from ``lang_sample`` -- usually the body of the triggering
    comment -- and format the result.

    Guessing wrong costs nothing but a reply in the other language; no mechanism
    or decision depends on it.
    """
    zh, en = _TEXTS[key]
    return (zh if looks_chinese(lang_sample) else en).format(**kw)
