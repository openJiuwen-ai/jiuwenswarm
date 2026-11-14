# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Use the ordinary Skill publishing channel for new SkillPack publications."""

from ..skill.skillpack_config import skillpack_productization_enabled
from .asset_publish_adapters import PublishValidationError


def skillpack_publish_profile() -> str:
    """Check the user setting before preparing or confirming a new publication."""
    if not skillpack_productization_enabled():
        raise PublishValidationError("SKILLPACK_DISABLED")
    return "legacy_skill_v1"
