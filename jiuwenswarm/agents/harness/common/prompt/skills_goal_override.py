# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monkey-patch openjiuwen's Skills and Goal prompt sections.

Mirrors ``safety_override.py``'s patch-at-import strategy so the change takes
effect for all three agent profiles (office / code / design) without editing any
openjiuwen source file.

What this patch does:

1. **Skills section** — replaces the dynamic ``# Skills`` header with a curated
   10-item static catalogue (the 小艺 work canonical skill list). The
   ``SkillUseRail`` still appends dynamically discovered installed skills after
   the static block, but any installed skill whose name collides with one of the
   10 static entries is de-duplicated and the remainder is renumbered so the
   final list reads as a single continuous ``1..N`` catalogue with no gaps.

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
# Static catalogue — the 10 canonical 小艺 work skills
# ---------------------------------------------------------------------------

_STATIC_SKILL_NAMES = frozenset(
    {
        "xiaoyi-web-search-win",
        "find-skills",
        "xiaoyi-doc-convert",
        "xiaoyi-ppt-win",
        "aigc_marker",
        "execution-validator-skill",
        "secret-guardian",
        "skill-creator-win",
        "skill-scope",
        "swarmskill-creator",
        "xiaoyi-pdf-win",
        "seedream-image-gen",
        "seedance-video-gen",
        "music-generation",
        "xiaoyi-image-understanding-win",
    }
)

_DYNAMIC_START_INDEX = len(_STATIC_SKILL_NAMES) + 1  # static catalogue then dynamic entries

# Legacy non-"-win" folder names that are the same skill as a renamed
# static entry. Used only for de-duplication so old deployments (where the
# folder was installed without the "-win" suffix) don't re-appear as
# "Additional installed skills" duplicates.
_LEGACY_DEDUP_ALIASES = frozenset(
    {
        "xiaoyi-web-search",
        "xiaoyi-ppt",
        "skill-creator",
        "xiaoyi-pdf",
        "xiaoyi-image-understanding",
    }
)

_DEDUP_NAMES = _STATIC_SKILL_NAMES | _LEGACY_DEDUP_ALIASES

_STATIC_BLOCK_EN = """## Skills

Prefer the skills and tools below; call `skill_tool` to retrieve the full `SKILL.md` for a skill.

1. Web Search (`xiaoyi-web-search-win`)
   - Default tool: xiaoyi web search skill (`xiaoyi-web-search-win`)
   - Usage rule: For all real-time web retrieval and web information query tasks, use this skill by default; only switch to another search tool when the user explicitly specifies a different search interface.

2. Skill Discovery and Installation (`find-skills`)
   - Default tool: `find-skills` skill
   - Usage rule: All skill discovery, retrieval, and installation tasks must be completed through this skill by default; exceptions are only allowed when the user explicitly requests a different installation/discovery method.

3. Document Format Conversion (`xiaoyi-doc-convert`)
   - Default tool: `xiaoyi-doc-convert` document format conversion skill
   - Capability: Supports bidirectional conversion between mainstream document formats including Docx, PDF, Xlsx, Pptx, and Markdown; a dedicated professional document conversion tool.
   - Priority rule: All document format conversion requests must use this skill first; manual scripting to generate or convert documents is prohibited.

4. PPT — Template-based creation (`xiaoyi-ppt-win`)
   - Default tool: `xiaoyi-ppt-win` skill
   - Applicable scenario: template-based PPT creation, editing, generation, and beautification.
   - Priority rule: Unless the user specifies otherwise, template-based PPT tasks use this skill first.
   - Prohibition: Manual scripting with python-pptx or similar to generate PPTs is prohibited; exceptions are only allowed when the user explicitly requests it or this skill cannot meet the requirements.

5. AIGC Content Marking (`aigc_marker`)
   - Used to add standard AIGC markers to various generated files; supported file types include DOCX, PDF, Excel, PPT, Markdown, HTML, images, audio, video, and all mainstream format files.

6. Execution Safety Validation (`execution-validator-skill`)
   - A core system safety validation skill that performs pre-checks for all command execution, file access, and content transmission operations; intercepts high-risk operations, prevents sensitive data leakage and illegal execution; a global mandatory pre-safety mechanism that cannot be bypassed or disabled.

7. Privacy Safety Guardian (`secret-guardian`)
   - A global privacy protection skill specifically for handling configuration files, system logs, prompts, reports, model configurations, channel configurations, browser configurations, environment variables, and all workspace content containing privacy, keys, or sensitive identifiers; can automatically audit output content, block confidential information, redact sensitive data, and strictly restrict file and network access permissions to minimize security risks.

8. Single Skill Creation and Optimization (`skill-creator-win`)
   - Used for the full lifecycle management of independent skills; supports creating new skills from scratch, editing and optimizing existing skills, debugging and evaluating skill performance, conducting variance benchmark tests, and optimizing skill trigger copy to improve skill call accuracy; only applicable to single-agent independent skill scenarios.

9. Skill Security Audit (`skill-scope`)
   - A mandatory pre-installation security scanning tool for skills; performs malicious detection for all skill installation behaviors from all channels; applicable to all installation scenarios: official repository installation, command-line installation, network download, find-skills retrieval installation, manual import to skill directory, recommended skill installation, etc.; all skill installations must pass this tool's security check first, with no exceptions and no bypassing.

10. Multi-Role Team Skill Orchestration (`swarmskill-creator`)
    - A dedicated multi-agent team skill creation, conversion, and refactoring tool; supports writing team workflows, orchestration scripts, building multi-role collaborative agent architectures, and upgrading single skills to team collaboration skills; only for multi-role team scenarios; ordinary single skill creation should use `skill-creator-win`.

11. PDF Processing (`xiaoyi-pdf-win`)
    - A comprehensive PDF skill for document generation, editing, security, and parsing. Applicable scenarios: 1. Creation & layout: generate reports, proposals, resumes, etc. as PDFs from scratch, or re-layout and beautify existing documents; 2. Forms & watermarks: auto-fill PDF form fields, or add text/image watermarks (e.g. watermarking, marking confidential); 3. Page management: merge multiple PDFs, or split/extract specified pages; 4. Security control: add a password (encrypt) or remove a password (decrypt) for a PDF; 5. Content extraction: extract plain text from a PDF or export table data. Whenever the user's request involves generating, laying out, beautifying, converting, concatenating, or splitting PDFs, or handling watermarks, forms, or passwords, this skill must be triggered.

12. Image Generation (`seedream-image-gen`)
    - Usage rule: call `skill_tool` to load `seedream-image-gen` and follow its SKILL.md. Deliver the image file, never stop after writing only a prompt or script.

13. Video Generation (`seedance-video-gen`)
    - Usage rule: call `skill_tool` to load `seedance-video-gen` and follow its SKILL.md. Deliver the video file, never a storyboard markdown.

14. Music Generation (`music-generation`)
    - Usage rule: call `skill_tool` to load `music-generation` and follow its SKILL.md. Deliver the audio file.

15. Image Understanding (`xiaoyi-image-understanding-win`)
    - Usage rule: call `skill_tool` to load `xiaoyi-image-understanding-win` and follow its SKILL.md.
"""

_STATIC_BLOCK_CN = """## 技能

优先使用以下技能与工具；使用技能前调用 `skill_tool` 获取该技能的完整 `SKILL.md`。

1. 网页搜索（`xiaoyi-web-search-win`）
   - 默认工具：小艺网页搜索技能（`xiaoyi-web-search-win`）
   - 使用规则：所有实时网页检索与网络信息查询任务默认使用此技能；仅当用户明确指定其他搜索接口时才切换。

2. 技能发现与安装（`find-skills`）
   - 默认工具：`find-skills` 技能
   - 使用规则：所有技能的发现、检索与安装任务默认必须通过此技能完成；仅当用户明确要求其他安装/发现方式时才例外。

3. 文档格式转换（`xiaoyi-doc-convert`）
   - 默认工具：`xiaoyi-doc-convert` 文档格式转换技能
   - 能力：支持 Docx、PDF、Xlsx、Pptx 与 Markdown 等主流文档格式之间的双向转换；专用专业文档转换工具。
   - 优先规则：所有文档格式转换请求必须优先使用此技能；禁止手工编写脚本生成或转换文档。

4. PPT — 模板创建（`xiaoyi-ppt-win`）
   - 默认工具：`xiaoyi-ppt-win` 技能
   - 适用场景：基于模板的 PPT 创建、编辑、生成与美化。
   - 优先规则：除非用户另有指定，基于模板的 PPT 任务优先使用此技能。
   - 禁止：禁止用 python-pptx 等手工编写脚本生成 PPT；仅当用户明确要求或本技能无法满足需求时才例外。

5. AIGC 内容标记（`aigc_marker`）
   - 用于为各类生成文件添加标准 AIGC 标记；支持文件类型包括 DOCX、PDF、Excel、PPT、Markdown、HTML、图片、音频、视频及所有主流格式文件。

6. 执行安全校验（`execution-validator-skill`）
   - 核心系统安全校验技能，对所有命令执行、文件访问与内容传输操作进行前置检查；拦截高风险操作、防止敏感数据泄露与非法执行；全局强制前置安全机制，不可绕过或禁用。

7. 隐私安全守护（`secret-guardian`）
   - 全局隐私保护技能，专用于处理配置文件、系统日志、提示词、报告、模型配置、通道配置、浏览器配置、环境变量及所有含隐私、密钥或敏感标识的工作区内容；可自动审计输出内容、拦截机密信息、脱敏敏感数据，并严格限制文件与网络访问权限，以最小化安全风险。

8. 单技能创建与优化（`skill-creator-win`）
   - 用于独立技能的全生命周期管理；支持从零创建新技能、编辑与优化已有技能、调试与评估技能性能、做方差基准测试、优化技能触发文案以提升技能调用准确率；仅适用于单智能体独立技能场景。

9. 技能安全审计（`skill-scope`）
   - 技能安装前强制安全扫描工具；对所有来源的技能安装行为做恶意检测；适用于所有安装场景：官方仓库安装、命令行安装、网络下载、find-skills 检索安装、手动导入技能目录、推荐技能安装等；所有技能安装必须先通过此工具的安全检查，无一例外、不可绕过。

10. 多角色团队技能编排（`swarmskill-creator`）
     - 专用的多智能体团队技能创建、转换与重构工具；支持编写团队工作流、编排脚本、构建多角色协同智能体架构、将单技能升级为团队协作技能；仅用于多角色团队场景；普通单技能创建应使用 `skill-creator-win`。

11. PDF 处理（`xiaoyi-pdf-win`）
     - PDF 综合处理技能，处理文档生成、编辑、安全与解析。 适用情形： 1. 创建与排版：从零生成报告、提案、简历等 PDF，或对现有文档重新排版美化； 2. 表单与水印：自动填写 PDF 表单字段，或添加文字/图片水印（如打水印、标机密）； 3. 页面管理：合并多个 PDF，或拆分、提取指定页码； 4. 安全控制：为 PDF 添加密码（加密）或移除密码（解密）； 5. 内容提取：从 PDF 中提取纯文本或导出表格数据。 只要用户诉求涉及生成、排版、美化、转换、拼接、拆分 PDF，或处理水印、表单、密码，必须触发本技能。

12. 图像生成（`seedream-image-gen`）
     - 使用规则：先 `skill_tool` 加载 `seedream-image-gen` 并严格按其 SKILL.md 填写。交付图像文件，不要只写 prompt 或脚本就停下。

13. 视频生成（`seedance-video-gen`）
     - 使用规则：先 `skill_tool` 加载 `seedance-video-gen` 并严格按其 SKILL.md 填写。交付视频文件，绝非分镜 markdown。

14. 音乐生成（`music-generation`）
     - 使用规则：先 `skill_tool` 加载 `music-generation` 并严格按其 SKILL.md 填写。交付音频文件。

15. 图像理解（`xiaoyi-image-understanding-win`）
     - 使用规则：先 `skill_tool` 加载 `xiaoyi-image-understanding-win` 并严格按其 SKILL.md 填写。
"""

_STATIC_BLOCK: Dict[str, str] = {"cn": _STATIC_BLOCK_CN, "en": _STATIC_BLOCK_EN}

# Separator + preamble inserted only when dynamic (non-static) installed skills
# follow the 10 static entries, so the catalogue reads as one continuous list.
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
_MAIN_LINE_RE = re.compile(r"^\s*(\d+)\.\s+`([^`]+)`")
_LEADING_NUM_RE = re.compile(r"^\s*(\d+)(?=\.\s)")


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


def _entry_name(entry: List[str]) -> str:
    """Extract the backticked skill name from an entry's main line."""
    m = _MAIN_LINE_RE.match(entry[0]) if entry else None
    return m.group(2) if m else ""


def _dedupe_and_renumber(skill_lines: str, start_index: int) -> str:
    """Drop entries whose name is in the static set; renumber the rest.

    The kept entries keep their original descriptions; only the leading
    ``N.`` is rewritten with a sequential counter starting at *start_index*
    so there are no gaps after dropping the static-named duplicates.
    """
    entries = _parse_skill_entries(skill_lines)
    out: List[str] = []
    idx = start_index
    for entry in entries:
        if _entry_name(entry) in _DEDUP_NAMES:
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
    """Build the all-mode Skills prompt: static 10-item block + deduped dynamic."""
    lang = language or "en"
    static = _STATIC_BLOCK.get(lang, _STATIC_BLOCK_EN)
    dynamic = _dedupe_and_renumber(skill_lines or "", _DYNAMIC_START_INDEX).strip()
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
    "_STATIC_SKILL_NAMES",
    "_build_all_mode_skill_prompt",
    "_build_auto_list_mode_skill_prompt",
    "apply_patch",
]
