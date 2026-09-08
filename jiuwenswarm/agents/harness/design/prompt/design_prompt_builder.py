"""Public fallback prompt builder for JiuwenSwarm's creative profile."""

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


class DesignPromptPriority(IntEnum):
    SAFETY = 13
    TONE_AND_STYLE = 31
    INTRO = 32
    SYSTEM = 11
    CORE_CAPABILITIES = 33


def _section(name: str, text: str, priority: int) -> PromptSection:
    return PromptSection(name=name, content={"en": text}, priority=priority)


def _design_intro_prompt() -> PromptSection:
    return _section("design_intro", "# Creative work\n\nHelp users plan and create visual or written deliverables.\n", DesignPromptPriority.INTRO)


def _design_safety_prompt() -> PromptSection:
    return _section("safety", safety_override.SAFETY_PROMPT_EN, DesignPromptPriority.SAFETY)


def _design_core_capabilities_prompt() -> PromptSection:
    return _section("design_core_capabilities", "# Capabilities\n\nUse available skills to create and refine requested deliverables.\n", DesignPromptPriority.CORE_CAPABILITIES)


def _design_system_prompt() -> PromptSection:
    return build_shared_system_section(priority=DesignPromptPriority.SYSTEM)


def _design_tone_and_style_prompt() -> PromptSection:
    return _section("design_tone_and_style", "# Tone and style\n\nCommunicate clearly and offer useful design guidance.\n", DesignPromptPriority.TONE_AND_STYLE)


_DESIGN_SECTION_GENERATORS = [
    build_shared_identity_section,
    build_shared_content_policy_section,
    _design_system_prompt,
    build_shared_regional_conventions_section,
    _design_safety_prompt,
    _design_intro_prompt,
    _design_core_capabilities_prompt,
    _design_tone_and_style_prompt,
]


def build_design_system_prompt() -> str:
    builder = SystemPromptBuilder(language="en")
    for section in build_design_system_prompt_sections():
        builder.add_section(section)
    return builder.build()


def build_design_system_prompt_sections() -> tuple[PromptSection, ...]:
    private_sections = load_mode_sections("creative")
    if private_sections is not None:
        return private_sections
    return tuple(generator() for generator in _DESIGN_SECTION_GENERATORS)


__all__ = ["DesignPromptPriority", "build_design_system_prompt", "build_design_system_prompt_sections"]
