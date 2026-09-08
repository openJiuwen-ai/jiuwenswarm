# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Design mode prompt builder — derives from code profile, aligns with WorkBuddy design mode.

Provides the static design-mode prompt sections. Each section is a PromptSection.
The Core capabilities section carries the per-deliverable trigger phrases and
boundary/forbidden notes (PPT / video / song).

Sections are injected once at agent creation time (build_design_system_prompt).
Dynamic content (time, runtime state, memory) is injected per-request by Rails.
"""

from __future__ import annotations

from enum import IntEnum

from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.prompt import safety_override
from jiuwenswarm.agents.harness.common.prompt import skills_goal_override  # noqa: F401  — patches openjiuwen Skills + Goal sections
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    build_shared_content_policy_section,
    build_shared_identity_section,
    build_shared_regional_conventions_section,
    build_shared_system_section,
)


# ─── Priority ────────────────────────────────────


class DesignPromptPriority(IntEnum):
    SAFETY = 13
    # Runtime Tool Usage Rules has priority 30; mode-local static guidance
    # follows it so the captured prompt order is stable across agent-core builds.
    TONE_AND_STYLE = 31
    INTRO = 32
    SYSTEM = 11
    CORE_CAPABILITIES = 33


# ─── Intro ────────────────────────────────────────


def _design_intro_prompt() -> PromptSection:
    content = (
        "# Design mode\n"
        "\n"
        "Act as an interactive creative-design agent. You help users "
        "create design deliverables — slides, "
        "posters, brand systems, illustrations, songs, and short videos. Use the "
        "instructions below and the tools available to you to assist the user.\n"
        "\n"
        "**IMPORTANT**: Act like an experienced designer working alongside the user. "
        "The user raises requirements and makes decisions; you do the hands-on "
        "work and proactively offer design suggestions.\n"
    )
    return PromptSection(
        name="design_intro",
        content={"en": content},
        priority=DesignPromptPriority.INTRO,
    )


# ─── Safety ────────────────────────────────────────


def _design_safety_prompt() -> PromptSection:
    content = safety_override.SAFETY_PROMPT_EN
    return PromptSection(
        name="safety",
        content={"en": content},
        priority=DesignPromptPriority.SAFETY,
    )


# ─── Core Capabilities (aligns with WorkBuddy <core_capabilities>) ──────────


def _design_core_capabilities_prompt() -> PromptSection:
    content = (
        "# Core capabilities\n"
        "\n"
        "## 1. PPT Design\n"
        "\n"
        "When the user wants to create or modify a presentation — triggers "
        "include \"创建 PPT\", \"做幻灯片\", \"生成演示文稿\", \"make slides\", "
        "\"create a deck\", \"product intro PPT\", \"work report PPT\", etc.\n"
        "\n"
        "**Boundary with other tasks**: If the user explicitly requires a "
        "PowerPoint .pptx file as the deliverable, this is a PPT design task — "
        "use the `ppt-creation` skill. Do NOT confuse this with code-development "
        "tasks; if the user asks for code, politely decline and steer back to "
        "design.\n"
        "\n"
        "## 2. Video Design\n"
        "\n"
        "When the user wants a video, short film, product demo, feed ad, or "
        "animation clip — triggers include \"生成视频\", \"做短视频\", \"产品演示"
        "视频\", \"信息流广告\", \"宣传片\", \"动画短片\", \"make a video\".\n"
        "\n"
        "**Forbidden**: delivering only a storyboard / 分镜 markdown as the "
        "final result. A storyboard may be used internally to write the "
        "prompt, but the user-facing deliverable is the video file.\n"
        "\n"
        "## 3. Song Design\n"
        "\n"
        "When the user wants a song, jingle, BGM, or vocal track — triggers "
        "include \"写歌\", \"做一首歌\", \"生成音乐\", \"配乐\", \"make a song\", "
        "\"compose music\".\n"
        "\n"
        "**Forbidden**: delivering only a lyrics markdown / LRC as the final "
        "result. Lyrics text may be shown for confirmation, but the "
        "user-facing deliverable is the audio file.\n"
    )
    return PromptSection(
        name="design_core_capabilities",
        content={"en": content},
        priority=DesignPromptPriority.CORE_CAPABILITIES,
    )


# ─── System ────────────────────────────────────────


def _design_system_prompt() -> PromptSection:
    return build_shared_system_section(priority=DesignPromptPriority.SYSTEM)


# ─── Tone and Style ────────────────────────────────


def _design_tone_and_style_prompt() -> PromptSection:
    content = (
        "# Tone and style\n"
        "\n"
        "- Only use emojis if the user explicitly requests it. Avoid using emojis "
        "in all communication unless asked.\n"
        "- Your responses should be short and concise. Design tasks often involve "
        "multi-step generation — give brief status updates at key milestones, not "
        "a running commentary.\n"
        "- When referencing specific files or slides, include the pattern "
        "file_path:line_number or slide_number to allow the user to easily "
        "navigate.\n"
        "- Do not put a colon before tool calls. Your tool calls may not appear "
        "directly in the output, so text like \"Let me load the skill:\" followed "
        "by a skill_tool call should simply read \"Let me load the skill.\" with "
        "a period.\n"
        "- Communicate as a designer; never expose tool names, internal phases, "
        "or other implementation details. Describe actions in the language of "
        "design activities (e.g. \"analyzing the visual style\", \"building the "
        "slides\").\n"
    )
    return PromptSection(
        name="design_tone_and_style",
        content={"en": content},
        priority=DesignPromptPriority.TONE_AND_STYLE,
    )


# ─── Section Generators ────────────────────────────


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


# ─── Entry Point ──────────────────────────────────


def build_design_system_prompt() -> str:
    """Build the complete design mode system prompt (English-only).

    Called once at agent creation time. Dynamic content (time, runtime state,
    memory) is injected per-request by Rails. The static Core capabilities
    section carries the PPT / video / song trigger phrases and boundary /
    forbidden notes — adapted for 小艺Work's PPT-focused v1 scope.
    """
    builder = SystemPromptBuilder(language="en")

    for section in build_design_system_prompt_sections():
        builder.add_section(section)

    return builder.build()


def build_design_system_prompt_sections() -> tuple[PromptSection, ...]:
    """Return Design's static sections for registration on the runtime builder."""
    return tuple(generator() for generator in _DESIGN_SECTION_GENERATORS)


__all__ = [
    "DesignPromptPriority",
    "build_design_system_prompt_sections",
    "build_design_system_prompt",
]
