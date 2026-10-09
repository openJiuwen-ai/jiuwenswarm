"""Public fallback prompt builder for JiuwenSwarm's code profile."""

from __future__ import annotations

from enum import IntEnum

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.prompt import safety_override
from jiuwenswarm.agents.harness.common.prompt import skills_goal_override  # noqa: F401
from jiuwenswarm.agents.harness.common.prompt.private_assets import load_mode_sections
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    build_shared_content_policy_section,
    build_shared_identity_section,
    build_shared_regional_conventions_section,
    build_shared_system_section,
)


class CodePromptPriority(IntEnum):
    SAFETY = 13
    TONE_AND_STYLE = 31
    INTRO = 32
    SYSTEM = 11
    DOING_TASKS = 33
    USING_YOUR_TOOLS = 34
    SESSION_GUIDANCE = 35
    ACTIONS_WITH_CARE = 36


def _section(name: str, text: str, priority: int) -> PromptSection:
    return PromptSection(name=name, content={"en": text}, priority=priority)


def _code_intro_prompt() -> PromptSection:
    return _section("code_intro", "# Code\n\nAssist with software tasks in the active workspace.\n", CodePromptPriority.INTRO)


def _code_safety_prompt() -> PromptSection:
    return _section("safety", safety_override.SAFETY_PROMPT_EN, CodePromptPriority.SAFETY)


def _code_system_prompt() -> PromptSection:
    return build_shared_system_section(priority=CodePromptPriority.SYSTEM)


def _code_session_guidance_prompt() -> PromptSection:
    return _section("code_session_guidance", "# Session guidance\n\nRead relevant code and documentation before making changes.\n", CodePromptPriority.SESSION_GUIDANCE)


def _code_doing_tasks_prompt() -> PromptSection:
    return _section("code_doing_tasks", "# Implementation\n\nMake focused changes, preserve existing behavior, and run relevant checks.\n", CodePromptPriority.DOING_TASKS)


def _code_using_your_tools_prompt() -> PromptSection:
    return _section("code_using_your_tools", "# Tools\n\nUse the available tools for inspection, editing, and verification.\n", CodePromptPriority.USING_YOUR_TOOLS)


def _code_actions_with_care_prompt() -> PromptSection:
    return _section("code_actions_with_care", "# Care\n\nConfirm consequential external actions and avoid destructive changes unless authorized.\n", CodePromptPriority.ACTIONS_WITH_CARE)


def _code_tone_and_style_prompt() -> PromptSection:
    return _section("code_tone_and_style", "# Tone and style\n\nBe concise and reference relevant source locations when useful.\n", CodePromptPriority.TONE_AND_STYLE)


_CODE_SECTION_GENERATORS = [
    build_shared_identity_section,
    build_shared_content_policy_section,
    _code_system_prompt,
    build_shared_regional_conventions_section,
    _code_safety_prompt,
    _code_intro_prompt,
    _code_doing_tasks_prompt,
    _code_using_your_tools_prompt,
    _code_session_guidance_prompt,
    _code_actions_with_care_prompt,
    _code_tone_and_style_prompt,
]


def build_code_system_prompt() -> str:
    builder = SystemPromptBuilder(language="en")
    for section in build_code_system_prompt_sections():
        builder.add_section(section)
    return builder.build()


def build_code_system_prompt_sections() -> tuple[PromptSection, ...]:
    private_sections = load_mode_sections("code")
    if private_sections is not None:
        return private_sections
    return tuple(generator() for generator in _CODE_SECTION_GENERATORS)


__all__ = ["CodePromptPriority", "build_code_system_prompt", "build_code_system_prompt_sections"]
