# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PromptSection factory for OfficeAceUserProfileRail."""
from __future__ import annotations

from typing import Optional

from openjiuwen.harness.prompts import PromptSection

# Section name used by both the factory and the Rail to add/remove.
# String literal (not ``SectionName.*``) so agent-core's enum stays untouched.
SECTION_NAME = "office_ace_user_profile"

_HEADER_CN = "## 用户画像（云端记忆概览，OfficeAceUserProfileRail 自动加载）"
_HEADER_EN = "## User Profile (cloud memory overview, auto-loaded by OfficeAceUserProfileRail)"

_NOTE_CN = (
    "以下内容来自云端记忆概览，反映跨端共享的用户长期画像。周期更新，"
    "修改以云端为准。"
)
_NOTE_EN = (
    "The following is the cloud memory overview, reflecting the user's "
    "cross-device long-term profile. Periodically updated; the cloud is the "
    "source of truth."
)


def build_office_ace_user_profile_section(
    content: str,
    *,
    priority: int = 130,
) -> Optional[PromptSection]:
    """Build the ``office_ace_user_profile`` :class:`PromptSection`, or ``None`` when empty.

    Both ``cn`` and ``en`` content keys are always populated so that whichever
    language the agent renders with, the header/note prose stays correct.
    """
    if not content or not content.strip():
        return None
    body = content.strip()
    body_cn = f"{_HEADER_CN}\n\n{_NOTE_CN}\n\n{body}\n"
    body_en = f"{_HEADER_EN}\n\n{_NOTE_EN}\n\n{body}\n"
    return PromptSection(
        name=SECTION_NAME,
        content={"cn": body_cn, "en": body_en},
        priority=priority,
    )


__all__ = ["build_office_ace_user_profile_section", "SECTION_NAME"]
