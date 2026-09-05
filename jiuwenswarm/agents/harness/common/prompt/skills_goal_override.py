# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monkey-patch openjiuwen's Skills and Goal prompt sections.

Mirrors ``safety_override.py``'s patch-at-import strategy so the change takes
effect for all three agent profiles (office / code / design) without editing any
openjiuwen source file.

What this patch does:

1. **Skills section** — replaces the dynamic ``# Skills`` header with a short
   static preamble (header + ``skill_tool`` guidance + the 小艺-first tool
   selection principle). No static skill catalogue is emitted. The
   ``SkillUseRail`` still appends dynamically discovered installed skills
   after the preamble; they are numbered as a single continuous ``1..N``
   list starting at 1. With no static titles to match, de-duplication is a
   no-op, but the dedupe/renumber pass is retained so the catalogue stays
   gap-free if a static catalogue is reintroduced later.

2. **Goal section** — the static ``# Goal 模式工作规则`` / ``Goal 上下文规则``
   protocol block (``_GOAL_PROTOCOL``) is emptied, and
   ``TaskCompletionRail.before_model_call`` is wrapped so the
   ``GOAL_PROTOCOL`` section is removed from the builder right after openjiuwen
   injects it. The dynamic Goal pieces (``<goal_task>`` XML,
   ``submit_goal_report``, the transcript assessor) are untouched, so Goal mode
   still runs — it just no longer carries the static protocol guidance.

Imported (idempotently) from ``prompt_builder.py`` (office) and
``code_prompt_builder.py`` (code/design) right next to ``safety_override``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from jiuwenswarm.common.utils import logger

# ---------------------------------------------------------------------------
# Static preamble — header + skill_tool guidance + 小艺-first principle
# ---------------------------------------------------------------------------

_STATIC_BLOCK_EN = """## Skills

Prefer the skills and tools below; call `skill_tool` to retrieve the full `SKILL.md` for a skill.

**Tool Selection Principle (xiaoyi First):** In all cases, unless the user explicitly specifies a different tool, you must prioritize using `小艺` related tools or skills whenever they are capable of completing the task.
"""

_STATIC_BLOCK_CN = """## 技能

优先使用以下技能与工具；使用技能前调用 `skill_tool` 获取该技能的完整 `SKILL.md`。

**工具选择原则（小艺优先）：** 除非用户明确指定其他工具，否则在所有情况下，只要小艺相关工具或技能能够完成任务，就必须优先使用。
"""

_STATIC_BLOCK: Dict[str, str] = {"cn": _STATIC_BLOCK_CN, "en": _STATIC_BLOCK_EN}

# Separator + preamble inserted only when dynamic (non-static) installed skills
# follow the static entries, so the catalogue reads as one continuous list.
_ADDITIONAL_HEADER_EN = "\n\nAdditional installed skills:\n\n"
_ADDITIONAL_HEADER_CN = "\n\n其他已安装技能：\n\n"
_ADDITIONAL_HEADER: Dict[str, str] = {"cn": _ADDITIONAL_HEADER_CN, "en": _ADDITIONAL_HEADER_EN}

# Fallback shown when no dynamic skills remain after de-duplication; the static
# block already ends with a trailing newline, so this just keeps a clean tail.
_TAIL_EN = "\n"
_TAIL_CN = "\n"
_TAIL: Dict[str, str] = {"cn": _TAIL_CN, "en": _TAIL_EN}

# ---------------------------------------------------------------------------
# De-duplication + renumbering for the dynamically rendered skill lines
# ---------------------------------------------------------------------------

# A rendered all-mode skill line looks like:
#   "{index}. `{skill_name}`{sep}{description}"  (+ optional "\n   Path: ..." continuation)
# Group 3 captures the description text (after the separator) used for
# description-based de-duplication against the static catalogue titles.
_MAIN_LINE_RE = re.compile(r"^\s*(\d+)\.\s+`([^`]+)`[：:]\s*(.*)$")
_LEADING_NUM_RE = re.compile(r"^\s*(\d+)(?=\.\s)")

# Matches a static catalogue header line: "N. Title" (no backticks).
_TITLE_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")


def _extract_titles(block: str) -> "frozenset[str]":
    """Extract entry titles from a static block (the text after ``N.``)."""
    titles: List[str] = []
    for line in (block or "").split("\n"):
        m = _TITLE_RE.match(line)
        if m:
            titles.append(m.group(1))
    return frozenset(titles)


# Per-language title sets derived from the static block. With the
# preamble-only static block these sets are empty, so the dedupe pass below is
# a no-op; the sets are retained so de-duplication resumes automatically if a
# static catalogue is reintroduced later.
_STATIC_TITLES: Dict[str, "frozenset[str]"] = {
    "en": _extract_titles(_STATIC_BLOCK_EN),
    "cn": _extract_titles(_STATIC_BLOCK_CN),
}

# Preamble contributes no numbered titles, so this resolves to 1 and dynamic
# installed skills number continuously from 1.
_DYNAMIC_START_INDEX = len(_STATIC_TITLES["en"]) + 1


def _parse_skill_entries(skill_lines: str) -> List[List[str]]:
    """Split a rendered skill_lines blob into per-entry line groups.

    Each entry is a list of lines: the first is the main numbered line, any
    following continuation lines (e.g. ``   Path: ...``) attach to it.
    """
    text = (skill_lines or "").strip()
    if not text:
        return []
    entries: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in text.split("\n"):
        if _MAIN_LINE_RE.match(line):
            if current is not None:
                entries.append(current)
            current = [line]
        else:
            if current is not None:
                current.append(line)
            # orphan continuation lines (no preceding main line) are ignored
    if current is not None:
        entries.append(current)
    return entries


def _entry_description(entry: List[str]) -> str:
    """Extract the description text from an entry's main line.

    Returns the text after the ``N. `name`` separator (group 3 of
    ``_MAIN_LINE_RE``); falls back to ``""`` when the line doesn't match.
    """
    m = _MAIN_LINE_RE.match(entry[0]) if entry else None
    return m.group(3) if m else ""


def _dedupe_and_renumber(
    skill_lines: str, start_index: int, language: str = "en"
) -> str:
    """Drop entries whose description matches a static block title; renumber the rest.

    A dynamic entry is dropped when its description text (the part after
    ``N. `name``) contains one of the static block titles as a
    case-insensitive substring. The kept entries keep their original
    descriptions; only the leading ``N.`` is rewritten with a sequential
    counter starting at *start_index* so there are no gaps. With the
    preamble-only static block the title set is empty, so nothing is dropped
    and the pass effectively just renumbers from *start_index*.
    """
    lang = language or "en"
    titles = _STATIC_TITLES.get(lang, _STATIC_TITLES["en"])
    entries = _parse_skill_entries(skill_lines)
    out: List[str] = []
    idx = start_index
    for entry in entries:
        desc = _entry_description(entry)
        if any(t.lower() in desc.lower() for t in titles):
            continue
        main_line = entry[0]
        rest = entry[1:]
        main_line = _LEADING_NUM_RE.sub(lambda _: str(idx), main_line, count=1)
        out.append("\n".join([main_line, *rest]))
        idx += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Patched builders (same signatures as openjiuwen's originals)
# ---------------------------------------------------------------------------

def _build_all_mode_skill_prompt(skill_lines: str, language: str = "en") -> str:
    """Build the all-mode Skills prompt: static preamble + renumbered dynamic."""
    lang = language or "en"
    static = _STATIC_BLOCK.get(lang, _STATIC_BLOCK_EN)
    dynamic = _dedupe_and_renumber(
        skill_lines or "", _DYNAMIC_START_INDEX, lang
    ).strip()
    if not dynamic:
        return static + _TAIL.get(lang, _TAIL_EN)
    return static + _ADDITIONAL_HEADER.get(lang, _ADDITIONAL_HEADER_EN) + dynamic + "\n"


def _build_auto_list_mode_skill_prompt(language: str = "en") -> str:
    """Auto-list mode: just the static block (no dynamic skill_lines available)."""
    lang = language or "en"
    return _STATIC_BLOCK.get(lang, _STATIC_BLOCK_EN) + _TAIL.get(lang, _TAIL_EN)


# ---------------------------------------------------------------------------
# Goal protocol — empty the static block and remove the section post-injection
# ---------------------------------------------------------------------------

_EMPTY_PROTOCOL = {"cn": "", "en": ""}


def _apply_goal_patch() -> None:
    """Empty ``_GOAL_PROTOCOL`` and wrap ``TaskCompletionRail.before_model_call``.

    The wrapper lets openjiuwen run its original injection logic, then removes
    the ``GOAL_PROTOCOL`` section from the builder so neither the
    ``# Goal 模式工作规则`` heading nor the ``Goal 上下文规则`` sub-block
    appears in the final system prompt. The reminder variant
    (``build_goal_reminder_section``) is dead code in the current openjiuwen
    build (no caller wires it), so removing by section name is safe.
    """
    try:
        import openjiuwen.harness.prompts.sections.goal as _goal
        _goal._GOAL_PROTOCOL = dict(_EMPTY_PROTOCOL)
    except Exception:
        logger.debug("[skills_goal_override] patch goal._GOAL_PROTOCOL failed", exc_info=True)

    try:
        from openjiuwen.harness.rails.task_completion_rail import TaskCompletionRail
        from openjiuwen.harness.prompts.sections import SectionName
    except Exception:
        logger.debug("[skills_goal_override] TaskCompletionRail import failed", exc_info=True)
        return

    if getattr(TaskCompletionRail.before_model_call, "__skills_goal_override_wrapped__", False):
        return

    _orig_before_model_call = TaskCompletionRail.before_model_call

    async def _patched_before_model_call(self, ctx) -> None:  # type: ignore[no-untyped-def]
        await _orig_before_model_call(self, ctx)
        builder = getattr(getattr(ctx, "agent", None), "system_prompt_builder", None)
        if builder is None:
            return
        try:
            builder.remove_section(SectionName.GOAL_PROTOCOL)
        except Exception:
            logger.debug(
                "[skills_goal_override] remove_section(GOAL_PROTOCOL) failed",
                exc_info=True,
            )

    _patched_before_model_call.__skills_goal_override_wrapped__ = True  # type: ignore[attr-defined]
    TaskCompletionRail.before_model_call = _patched_before_model_call  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Apply skills patch (module attrs + defensive skill_use_rail namespace patch)
# ---------------------------------------------------------------------------

_PATCHED = False


def apply_patch() -> None:
    """Patch openjiuwen's skills + goal prompt sections. Idempotent."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # 1. Patch the skills module's public builders.
    try:
        import openjiuwen.harness.prompts.sections.skills as _skills
        _skills.build_all_mode_skill_prompt = _build_all_mode_skill_prompt
        _skills.build_auto_list_mode_skill_prompt = _build_auto_list_mode_skill_prompt
        # Keep the no-skill fallback consistent: it now returns the static block.
        _skills.SKILL_RAIL_NO_SKILL_PROMPT = {
            "cn": _build_auto_list_mode_skill_prompt("cn"),
            "en": _build_auto_list_mode_skill_prompt("en"),
        }
    except Exception:
        logger.debug("[skills_goal_override] patch skills module failed", exc_info=True)

    # 2. Defensive: if SkillUseRail already captured the originals via a
    #    top-level ``from ... import``, rebind those names in its namespace too.
    try:
        import openjiuwen.harness.rails.skills.skill_use_rail as _sur
        _sur.build_all_mode_skill_prompt = _build_all_mode_skill_prompt
        _sur.build_auto_list_mode_skill_prompt = _build_auto_list_mode_skill_prompt
    except Exception:
        # Not imported yet — the skills-module patch above will be picked up by
        # SkillUseRail's own ``from ... import`` whenever it loads later.
        pass

    # 3. Goal section removal.
    _apply_goal_patch()


apply_patch()


__all__ = [
    "_build_all_mode_skill_prompt",
    "_build_auto_list_mode_skill_prompt",
    "apply_patch",
]
