"""Hide the catalog and adapt legacy Skill entry instructions for retrieval."""

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections.skills import build_skills_section


def _adapt_entry(text: str) -> str:
    # Limit replacements to the recognized native Skill section, never to the
    # general file-tool instructions or user messages. Both SDK languages and
    # the empty-catalog variant contain legacy read_file entry instructions.
    replacements = (
        ("执行前先用 read_file 阅读相关 SKILL.md。", "执行前按技能检索补充说明加载相关 SKILL.md。"),
        ("可用技能：", "技能目录通过检索按需提供。"),
        ("当前任务没有选择任何技能。如有技能信息可用，请用 read_file 阅读相关 SKILL.md。",
         "当前任务没有选择任何技能。需要技能时，按技能检索补充说明查找并加载 SKILL.md。"),
        ("Read the relevant SKILL.md using read_file before execution.",
         "Load the relevant SKILL.md through the skill retrieval guidance before execution."),
        ("Available skills:", "The skill catalog is available through on-demand retrieval."),
        ("No skill was selected for this task. When skill information is available, "
         "read the relevant SKILL.md using read_file.",
         "No skill was selected for this task. When a skill is needed, "
         "find and load its SKILL.md through the skill retrieval guidance."),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def without_catalog(section: PromptSection) -> PromptSection:
    # Ask the installed SDK for its boundaries rather than copying its rules.
    # Only a recognized all-mode catalog may be removed. An incompatible SDK
    # falls back through the caller instead of silently discarding native rules.
    marker = "__flash_skill_catalog_slot__"
    content = {}
    for language, original in section.content.items():
        template = build_skills_section(skill_lines=marker, language=language, mode="all")
        empty = build_skills_section(skill_lines="", language=language, mode="all")
        if original == empty.render(language):
            content[language] = _adapt_entry(original)
            continue
        before, after = template.render(language).split(marker)
        if not before or not after:
            raise ValueError("Native Skill prompt format is not supported")
        if (not original.startswith(before) or not original.endswith(after)
                or len(original) < len(before) + len(after)):
            raise ValueError("Native Skill prompt format is not supported")
        content[language] = _adapt_entry(before + after)
    return PromptSection(name=section.name, content=content, priority=section.priority)
