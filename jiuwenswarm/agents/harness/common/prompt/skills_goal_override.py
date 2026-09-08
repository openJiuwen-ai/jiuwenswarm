# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monkey-patch openjiuwen's Skills and Goal prompt sections.

Mirrors ``safety_override.py``'s patch-at-import strategy so the change takes
effect for all three agent profiles (office / code / design) without editing any
openjiuwen source file.

What this patch does:

1. **Skills section** — keeps the upstream 小艺-first policy as stable prompt
   text, then renders currently loaded installed skills directly from their
   structured ``Skill`` objects rather than reparsing Markdown. A bounded
   catalogue keeps whole descriptions first, then preserves later skill names
   in compact form for on-demand discovery.

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

import os
from html import escape
from typing import Any, Dict, List, Sequence

import openjiuwen.harness.prompts.sections.context as _context
import openjiuwen.harness.prompts.sections.skills as _skills
import openjiuwen.harness.rails.skills.skill_use_rail as _sur
from openjiuwen.harness.prompts.builder import PromptSection
from openjiuwen.harness.prompts.sections import SectionName

from jiuwenswarm.common.utils import logger

# ---------------------------------------------------------------------------
# Skills preamble and dynamic catalogue
# ---------------------------------------------------------------------------

# Final system-prompt budget for the Skills section. This lives in the product
# override rather than agent-core so packaged/runtime dependency refreshes do
# not erase the policy. Set to 0 to disable the cap for troubleshooting.
_SKILLS_PROMPT_MAX_CHARS_ENV = "JIUWENSWARM_SKILLS_PROMPT_MAX_CHARS"
_DEFAULT_SKILLS_PROMPT_MAX_CHARS = 30_000

# Keep Skills after every static Code/Design prompt section (the highest is
# output efficiency at 50), but before per-request runtime context (60+).
# This applies to both all-mode and auto-list mode; upstream assigns 40 to the
# latter unless the rail result is normalized below.
_SKILLS_SECTION_PRIORITY = 56

# Keep only the upstream 小艺-first policy as stable prompt text; every skill
# entry is sourced from the rail's currently loaded user skills.
_SKILLS_PREAMBLE_EN = """# Skills

Prefer the skills and tools below; call `skill_tool` to retrieve the full `SKILL.md` for a skill.

**Tool Selection Principle (xiaoyi First):** In all cases, unless the user explicitly specifies a different tool, you must prioritize using `小艺` related tools or skills whenever they are capable of completing the task.
"""

_SKILLS_PREAMBLE_CN = """# 技能

优先使用以下技能与工具；使用技能前调用 `skill_tool` 获取该技能的完整 `SKILL.md`。

**工具选择原则（小艺优先）：** 除非用户明确指定其他工具，否则在所有情况下，只要小艺相关工具或技能能够完成任务，就必须优先使用。
"""

_SKILLS_PREAMBLE: Dict[str, str] = {
    "cn": _SKILLS_PREAMBLE_CN,
    "en": _SKILLS_PREAMBLE_EN,
}
_STATIC_BLOCK_EN = _SKILLS_PREAMBLE_EN + "\n<available_skills>\n</available_skills>\n"
_STATIC_BLOCK_CN = _SKILLS_PREAMBLE_CN + "\n<available_skills>\n</available_skills>\n"
_STATIC_BLOCK = {"cn": _STATIC_BLOCK_CN, "en": _STATIC_BLOCK_EN}

_AVAILABLE_SKILLS_CLOSE_TAG = "</available_skills>"

_FIND_SKILLS_TOOL_RULES = {
    "cn": """## 技能发现与安装（`find-skills-win`）

- **默认工具：** `find-skills-win` 技能。
- **使用规则：** 所有技能发现、检索和安装任务默认必须通过该技能完成；仅当用户明确要求其他发现或安装方式时，才可使用其他方法。""",
    "en": """## Skill Discovery and Installation (`find-skills-win`)

- **Default tool:** the `find-skills-win` skill.
- **Usage rule:** Complete all skill discovery, retrieval, and installation tasks through this skill by default. Use another discovery or installation method only when the user explicitly requests it.""",
}

# Keep product wording stable across agent-core releases.  Calling the upstream
# builder below still determines whether the current request has any tools;
# only the displayed policy text is owned here.
_TOOL_USAGE_RULES = {
    "cn": """# 工具使用规则

- 只调用当前请求中实际可用的工具。
- 相同工具和相同参数已有结果时，不要重复调用。
- 上一次结果为空或没有新增信息时，调整参数、改用其他工具或说明结果不足。
- 文件搜索、读取、编辑和写入优先使用专用工具，不要用 Shell 重复实现。
- Shell 命令只有存在依赖关系时才串联；长时间运行的命令应根据需要增大 `timeout`，不要用 `sleep` 轮询。""",
    "en": """# Tool Usage Rules

- Only call tools that are actually available in the current request.
- Do not repeat the same tool with the same parameters when a result already exists.
- If the previous result is empty or has no new information, adjust parameters, switch to another tool, or state that the result is insufficient.
- Prefer dedicated tools for file search, read, edit, and write — do not reimplement them with Shell.
- Chain Shell commands only when there are dependencies; for long-running commands, increase `timeout` as needed — do not poll with `sleep`.""",
}

def _skills_prompt_max_chars() -> int:
    """Return the configured final Skills-section character budget.

    Invalid and negative values retain the safe default. A value of ``0`` is
    an explicit opt-out for troubleshooting; this must not be the default
    because installed skill descriptions originate outside the static prompt.
    """
    raw = os.environ.get(_SKILLS_PROMPT_MAX_CHARS_ENV, "").strip()
    if not raw:
        return _DEFAULT_SKILLS_PROMPT_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default %d",
            _SKILLS_PROMPT_MAX_CHARS_ENV,
            raw,
            _DEFAULT_SKILLS_PROMPT_MAX_CHARS,
        )
        return _DEFAULT_SKILLS_PROMPT_MAX_CHARS
    if value < 0:
        logger.warning(
            "Negative %s=%r; using default %d",
            _SKILLS_PROMPT_MAX_CHARS_ENV,
            raw,
            _DEFAULT_SKILLS_PROMPT_MAX_CHARS,
        )
        return _DEFAULT_SKILLS_PROMPT_MAX_CHARS
    return value


def _visible_dynamic_skills(skills: Sequence[Any]) -> List[Any]:
    """Return every loaded skill once, in its existing stable order."""
    visible: List[Any] = []
    seen_names: set[str] = set()
    for skill in skills:
        name = str(getattr(skill, "name", "") or "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        visible.append(skill)
    return visible


def _dynamic_skill_entries_xml(skills: Sequence[Any], *, compact: bool = False) -> List[str]:
    """Render dynamic ``Skill`` objects directly, never reparsing Markdown.

    ``Skill.description`` can legitimately contain numbered examples. Rendering
    the objects at this boundary avoids mistaking those examples for skills.
    """
    rendered: List[str] = []
    for skill in skills:
        name = str(getattr(skill, "name", "") or "").strip()
        if not name:
            continue
        if compact:
            rendered.append(f"  <skill><name>{escape(name)}</name></skill>")
            continue
        description = str(getattr(skill, "description", "") or "").strip()
        rendered.append(
            "  <skill>\n"
            f"    <name>{escape(name)}</name>\n"
            f"    <description>{escape(description)}</description>\n"
            "  </skill>"
        )
    return rendered


def _append_until_budget(
    entries: Sequence[str],
    *,
    initial_length: int,
    max_chars: int,
) -> tuple[List[str], int]:
    """Append whole entries in stable order, returning entries and final size."""
    kept: List[str] = []
    current_length = initial_length
    for candidate in entries:
        separator_length = 1 if kept else 0
        if current_length + separator_length + len(candidate) > max_chars:
            break
        kept.append(candidate)
        current_length += separator_length + len(candidate)
    return kept, current_length


# ---------------------------------------------------------------------------
# Patched builders (same signatures as openjiuwen's originals)
# ---------------------------------------------------------------------------

def _build_all_mode_skill_prompt_from_skills(
    skills: Sequence[Any], language: str = "en"
) -> str:
    """Build a bounded XML catalogue from structured dynamic skills.

    Full descriptions are kept only as whole entries. Once the budget is hit,
    later dynamic skills downgrade to name-only entries, so the model can still
    discover and load them. A compact notice directs broad capability discovery
    to the already-installed ``find-skills-win`` skill.
    """
    lang = language or "en"
    static = _STATIC_BLOCK.get(lang, _STATIC_BLOCK_EN)
    prefix, _ = static.rsplit(_AVAILABLE_SKILLS_CLOSE_TAG, 1)
    prefix = prefix.rstrip() + "\n"
    suffix = _AVAILABLE_SKILLS_CLOSE_TAG + "\n"
    visible_skills = _visible_dynamic_skills(skills)
    full_entries = _dynamic_skill_entries_xml(visible_skills)
    if not full_entries:
        return prefix + suffix

    configured_max = _skills_prompt_max_chars()
    if configured_max == 0:
        return prefix + "\n".join(full_entries) + "\n" + suffix

    static_length = len(prefix) + len(suffix)
    max_chars = max(configured_max, static_length)
    if configured_max < static_length:
        logger.warning(
            "%s=%d is smaller than the required Skills preamble (%d); using %d",
            _SKILLS_PROMPT_MAX_CHARS_ENV,
            configured_max,
            static_length,
            static_length,
        )

    full_kept, current_length = _append_until_budget(
        full_entries,
        initial_length=static_length,
        max_chars=max_chars,
    )
    if len(full_kept) == len(full_entries):
        return prefix + "\n".join(full_kept) + "\n" + suffix

    notice = (
        "  <catalog_notice>Some dynamic skill descriptions are omitted to stay within "
        "the prompt budget.</catalog_notice>"
    )
    # Keep the notice only when it does not displace a skill identity.
    notice_length = len(notice) + (1 if full_kept else 0)
    compact_entries = _dynamic_skill_entries_xml(
        visible_skills[len(full_kept) :], compact=True
    )
    compact_kept, _ = _append_until_budget(
        compact_entries,
        initial_length=current_length + notice_length,
        max_chars=max_chars,
    )
    parts = [*full_kept]
    if compact_kept or len(full_kept) < len(full_entries):
        if current_length + notice_length <= max_chars:
            parts.append(notice)
            current_length += notice_length
        compact_kept, _ = _append_until_budget(
            compact_entries,
            initial_length=current_length,
            max_chars=max_chars,
        )
        parts.extend(compact_kept)

    compact_omitted = len(compact_entries) - len(compact_kept)
    logger.warning(
        "Skills prompt reached %d-char budget; kept %d full, %d name-only, omitted %d "
        "dynamic skill(s)",
        max_chars,
        len(full_kept),
        len(compact_kept),
        compact_omitted,
    )
    return prefix + ("\n".join(parts) + "\n" if parts else "") + suffix


def _build_auto_list_mode_skill_prompt(language: str = "en") -> str:
    """Auto-list mode: keep the stable preamble; discover skills on demand."""
    lang = language or "en"
    return _SKILLS_PREAMBLE.get(lang, _SKILLS_PREAMBLE_EN) + "\n"


# ---------------------------------------------------------------------------
# Tool-usage section — retain find-skills-win policy next to other tool rules
# ---------------------------------------------------------------------------

# Safety is static priority 13 in all three modes.  Priority 14 reserves its
# immediate successor for this runtime-built section.  Product static sections
# that must follow Tools use 31+ so the ordering also stays correct when an
# older agent-core writer leaves Tools at its upstream priority 30.
_TOOL_USAGE_SECTION_PRIORITY = 14

def _build_tools_content_with_find_skills(ability_manager, language: str = "cn") -> str | None:
    """Build product tool rules plus skill-discovery policy when tools exist."""
    # Preserve upstream's availability check, but keep the product rule wording
    # independent from agent-core prompt-text changes.
    if not _ORIGINAL_BUILD_TOOLS_CONTENT(ability_manager, language):
        return None
    lang = language or "cn"
    content = _TOOL_USAGE_RULES.get(lang, _TOOL_USAGE_RULES["cn"])
    rule = _FIND_SKILLS_TOOL_RULES.get(lang, _FIND_SKILLS_TOOL_RULES["en"])
    return f"{content.rstrip()}\n\n{rule}\n"


_ORIGINAL_BUILD_TOOLS_CONTENT = _context.build_tools_content
_ORIGINAL_BUILD_TOOLS_SECTION = _context.build_tools_section


def _build_tools_section_after_safety(ability_manager, language: str = "cn"):
    """Build Tool Usage Rules directly after the shared Safety section."""
    section = _ORIGINAL_BUILD_TOOLS_SECTION(ability_manager, language)
    if section is not None:
        section.priority = _TOOL_USAGE_SECTION_PRIORITY
    return section


def _apply_tools_patch() -> None:
    """Patch shared tool text without changing the public API.

    Product-owned rails apply the final section priority locally. Do not modify
    ``ContextAssembleRail`` globally here: unrelated agents sharing this
    process must retain their upstream behavior.
    """
    if not getattr(_context.build_tools_content, "__skills_goal_override_wrapped__", False):
        _build_tools_content_with_find_skills.__skills_goal_override_wrapped__ = True  # type: ignore[attr-defined]
        _build_tools_section_after_safety.__skills_goal_override_wrapped__ = True  # type: ignore[attr-defined]
        _context.build_tools_content = _build_tools_content_with_find_skills
        _context.build_tools_section = _build_tools_section_after_safety


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
# Apply skills patch (structured SkillUseRail override + auto-list prompt)
# ---------------------------------------------------------------------------

_PATCHED = False


def apply_patch() -> None:
    """Patch openjiuwen's skills + goal prompt sections. Idempotent."""
    global _PATCHED
    if _PATCHED:
        return

    # 1. Auto-list has no dynamic entries, so a module-level builder patch is
    # sufficient. All-mode is patched below at the structured Skill boundary.
    # Fail fast like safety_override: an agent-core API mismatch must be visible
    # during startup rather than silently changing prompt behavior.
    _skills.build_auto_list_mode_skill_prompt = _build_auto_list_mode_skill_prompt
    _skills.SKILL_RAIL_NO_SKILL_PROMPT = {
        "cn": _build_auto_list_mode_skill_prompt("cn"),
        "en": _build_auto_list_mode_skill_prompt("en"),
    }

    # 2. Preserve Skill objects until their XML rendering boundary. The upstream
    # rail first renders Markdown; parsing it again is ambiguous for multi-line
    # descriptions that contain numbered examples.
    original_build = _sur.SkillUseRail._build_skills_section
    if not getattr(original_build, "__skills_goal_override_wrapped__", False):

        def _patched_build_skills_section(self, skills=None):  # type: ignore[no-untyped-def]
            if self.skill_mode != self.SKILL_MODE_ALL:
                section = original_build(self, skills)
                if section is None:
                    return None
                return PromptSection(
                    name=section.name,
                    content=section.content,
                    priority=_SKILLS_SECTION_PRIORITY,
                )
            current_skills = self.skills if skills is None else skills
            builder = getattr(self, "system_prompt_builder", None)
            language = getattr(builder, "language", "en") or "en"
            return PromptSection(
                name=SectionName.SKILLS,
                content={
                    language: _build_all_mode_skill_prompt_from_skills(
                        current_skills, language
                    )
                },
                priority=_SKILLS_SECTION_PRIORITY,
            )

        _patched_build_skills_section.__skills_goal_override_wrapped__ = True  # type: ignore[attr-defined]
        _sur.SkillUseRail._build_skills_section = _patched_build_skills_section

    # 3. Keep skill discovery policy in the dynamic Tool Usage Rules section.
    _apply_tools_patch()

    # 4. Goal section removal.
    _apply_goal_patch()
    _PATCHED = True


apply_patch()


__all__ = [
    "_build_all_mode_skill_prompt_from_skills",
    "_build_auto_list_mode_skill_prompt",
    "apply_patch",
]
