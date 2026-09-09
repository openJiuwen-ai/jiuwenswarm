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

Prefer the skills below; call `skill_tool` to retrieve the full `SKILL.md` for a skill.

Skill Usage Principle (xiaoyi First): In all cases, unless the user explicitly specifies a different skill, prioritize using xiaoyi related skills whenever they are capable of completing the task.
"""

_SKILLS_PREAMBLE_CN = """# 技能

优先使用以下技能；使用技能前调用 `skill_tool` 获取该技能的完整 `SKILL.md`。

技能选择原则（小艺优先）： 除非用户明确指定其他技能，否则在所有情况下，只要小艺相关技能能够完成任务，就必须优先使用。
"""

_SKILLS_PREAMBLE: Dict[str, str] = {
    "cn": _SKILLS_PREAMBLE_CN,
    "en": _SKILLS_PREAMBLE_EN,
}
_STATIC_BLOCK_EN = _SKILLS_PREAMBLE_EN + "\n<available_skills>\n</available_skills>\n"
_STATIC_BLOCK_CN = _SKILLS_PREAMBLE_CN + "\n<available_skills>\n</available_skills>\n"
_STATIC_BLOCK = {"cn": _STATIC_BLOCK_CN, "en": _STATIC_BLOCK_EN}

_AVAILABLE_SKILLS_CLOSE_TAG = "</available_skills>"

# Merged Tool Usage Rules: basic principles + dedicated-tool mapping +
# task planning + parallel calls + bash/git safety + find-skills policy
# (inlined as plain bullets — no subsection). Shared by all three modes
# (office / code / design) via the runtime Tool Usage Rules section (P14).
_TOOL_USAGE_RULES = {
    "cn": """# 工具使用规则

- 只调用当前请求中实际可用的工具。
- 相同工具和相同参数已有结果时，不要重复调用。
- 上一次结果为空或没有新增信息时，调整参数、改用其他工具或说明结果不足。
- 文件搜索、读取、编辑和写入优先使用专用工具，不要用 Shell 重复实现。
- Shell 命令只有存在依赖关系时才串联；长时间运行的命令应根据需要增大 `timeout`，不要用 `sleep` 轮询。
- 技能发现与安装默认工具：`find-skills-win` 技能。所有技能发现、检索和安装任务默认必须通过该技能完成；仅当用户明确要求其他发现或安装方式时，才可使用其他方法。

当有相关专用工具时，不要用 bash 运行命令。使用专用工具能让用户更好地理解和审查你的工作。这对协助用户至关重要：
- 读取文件用 read_file，不要用 cat、head、tail 或 sed
- 编辑文件用 edit_file，不要用 sed 或 awk
- 创建文件用 write_file，不要用 cat heredoc 或 echo 重定向
- 搜索文件用 glob 或 list_files，不要用 find 或 ls
- 搜索文件内容用 grep，不要用 bash 的 grep 命令
- bash 用于普通 POSIX 系统命令和 Bash 脚本。powershell 用于 Windows 原生 cmdlet 和路径。mcp_exec_command 仅在需要显式 Shell 参数化、后台执行或专用 Shell 工具时使用；每次 mcp_exec_command 调用必须包含 shell_type=\"bash\"、\"powershell\"、\"cmd\" 或 \"sh\"，不能使用 auto。如果不确定且有相关专用文件工具，默认使用该专用工具。

## 任务规划（todos）

使用 todo 工具分解和管理任何涉及工具调用或任务分解的任务。仅纯简单对话（一次性问题或闲聊，无工具调用）可跳过 todos。
- 仅对无需工具调用的简单对话任务跳过 todos。
- 中等工作量（例如全新后端 + 前端 + 验证）：2–3 个基于结果的里程碑，不是每个文件或规范章节一个条目。
- 复杂工作量（多个交付物、大型重构、顺序不明）：最多 4–6 个里程碑。
- 在实质性工作前调用一次 todo_create；优先与第一次 write/bash 并行，不要单独一轮 todo。
- 尽可能在下一工作工具的同一响应中通过 todo_modify 标记里程碑完成；批量状态更新；避免单独一轮 todo。
- 不要例行调用 todo_list。将验证保留在最终里程碑中，不要每个检查单独建 todo。

## 并行工具调用

你可以在单个响应中调用多个工具。如果你打算调用多个工具且它们之间没有依赖关系，将所有独立的工具调用一起发出。尽可能使用并行工具调用以提高效率。但当某些调用依赖前面调用产生的值时，不要并行运行；而是一个接一个地顺序运行。例如，如果一个操作必须在另一个开始之前完成，则顺序执行这些操作。

## Bash 使用规则

- 工作目录在命令之间持久存在，但 shell 状态不会。
- 当命令共享上下文或顺序重要时，优先一次 bash 调用完成一个工作步骤。在单次 bash 调用中用 && 串联依赖命令；仅当早期失败不应阻止后续步骤时才用 ;。
- 不要将依赖验证分散到多轮。启动服务器、等待并 HTTP 测试在一次调用中完成，例如 `python app.py & sleep 3 && curl http://localhost:5000/`。
- 当一个响应中需要多次 bash 调用时，仅并行化真正独立的操作（例如 `git status` 和 `git diff`）。不要并行化属于同一检查的设置、验证或清理。
- 仅当上一条命令失败且需要不同诊断或修复时，才使用单独一轮 bash。
- 不要在单次 bash 调用中用换行符分隔命令（引号字符串内换行可以）。
- 启动后台进程后的短暂 sleep 在同一串联命令中可以接受；不要用 sleep 重试循环掩盖失败。

### Git 安全协议

- 永远不要更新 git config
- 永远不要运行破坏性 git 命令（push --force、reset --hard、checkout .、restore .、clean -f、branch -D），除非用户明确要求这些操作。
- 永远不要跳过 hooks（--no-verify、--no-gpg-sign 等），除非用户明确要求
- 永远不要 force push 到 main/master，如果用户要求则警告
- 关键：始终创建新提交而不是修正（amend），除非用户明确要求 git amend。
- 暂存文件时，优先按名称添加特定文件，而不是使用 \"git add -A\" 或 \"git add .\"
- 永远不要提交更改，除非用户明确要求。
- 永远不要运行交互式 git 命令（例如 git rebase -i、git add -i）。""",
    "en": """# Tool Usage Rules

- Only call tools that are actually available in the current request.
- Do not repeat the same tool with the same parameters when a result already exists.
- If the previous result is empty or has no new information, adjust parameters, switch to another tool, or state that the result is insufficient.
- Prefer dedicated tools for file search, read, edit, and write — do not reimplement them with Shell.
- Chain Shell commands only when there are dependencies; for long-running commands, increase `timeout` as needed — do not poll with `sleep`.
- Default tool for skill discovery and installation: the `find-skills-win` skill. Complete all skill discovery, retrieval, and installation tasks through this skill by default; use another method only when the user explicitly requests it.

Do NOT use bash to run commands when a relevant dedicated tool is provided. Using dedicated tools allows the user to better understand and review your work. This is CRITICAL to assisting the user:
- To read files use read_file instead of cat, head, tail, or sed
- To edit files use edit_file instead of sed or awk
- To create files use write_file instead of cat with heredoc or echo redirection
- To search for files use glob or list_files instead of find or ls
- To search the content of files, use grep instead of the bash grep command
- Use bash for ordinary POSIX system commands and Bash scripts. Use powershell for Windows-native cmdlets and paths. Use mcp_exec_command only when explicit Shell parameterization, background execution, or a dedicated Shell tool is needed; every mcp_exec_command call must include shell_type=\"bash\", \"powershell\", \"cmd\", or \"sh\" and must not use auto. If you are unsure and there is a relevant dedicated file tool, default to using that dedicated tool.

## Task planning (todos)

Use the todo tools to break down and manage any task that involves tool calls or task decomposition. Only pure simple conversation (one-off questions or small talk with no tool calls) may skip todos.
- Skip todos ONLY for simple conversational tasks that need no tool calls.
- Medium work (e.g. greenfield backend + frontend + verify): 2–3 outcome-based milestones, not one item per file or spec section.
- Complex work (many deliverables, large refactor, unclear order): 4–6 milestones max.
- Call todo_create once before substantive work; prefer parallel with the first write/bash, not a todo-only round.
- Mark milestones completed via todo_modify in the same response as the next work tool when possible; batch status updates; avoid todo-only rounds.
- Do not call todo_list routinely. Keep verification in the final milestone, not separate todos per check.

## Parallel tool calls

You can call multiple tools in a single response. If you intend to call multiple tools and there are no dependencies between them, issue all of the independent tool calls together. Use parallel tool calls wherever you can to work more efficiently. But when some calls rely on values produced by earlier calls, do NOT run them in parallel; run them one after another instead. For example, if one operation must finish before another begins, execute those operations sequentially.

## Bash usage rules

- Working directory persists between commands, but shell state does not.
- Prefer one bash call per workflow step when commands share context or order matters. Chain dependent commands with && in a single bash call; use ; only when earlier failures should not block later steps.
- Do NOT split dependent verification across multiple rounds. Start server, wait, and HTTP-test in one call, e.g. `python app.py & sleep 3 && curl http://localhost:5000/`.
- When multiple bash calls are needed in one response, parallelize only truly independent operations (e.g. `git status` and `git diff`). Do not parallelize setup, verification, or cleanup that belong to the same check.
- Use a separate bash round only when the previous command failed and you need a different diagnostic or fix.
- Do not use newlines to separate commands in a single bash call (newlines are ok in quoted strings).
- A short sleep after starting a background process is fine within the same chained command; do not use sleep-retry loops to mask failures.

### Git Safety Protocol

- NEVER update the git config
- NEVER run destructive git commands (push --force, reset --hard, checkout ., restore ., clean -f, branch -D) unless the user explicitly requests these actions.
- NEVER skip hooks (--no-verify, --no-gpg-sign, etc) unless the user explicitly requests it
- NEVER run force push to main/master, warn the user if they request it
- CRITICAL: Always create NEW commits rather than amending, unless the user explicitly requests a git amend.
- When staging files, prefer adding specific files by name rather than using \"git add -A\" or \"git add .\"
- NEVER commit changes unless the user explicitly asks you to.
- Never run interactive git commands (e.g. git rebase -i, git add -i).""",
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
    """Build merged tool rules (incl. find-skills policy) when tools exist."""
    # Preserve upstream's availability check, but keep the product rule wording
    # independent from agent-core prompt-text changes.
    if not _ORIGINAL_BUILD_TOOLS_CONTENT(ability_manager, language):
        return None
    lang = language or "cn"
    return _TOOL_USAGE_RULES.get(lang, _TOOL_USAGE_RULES["cn"])


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
