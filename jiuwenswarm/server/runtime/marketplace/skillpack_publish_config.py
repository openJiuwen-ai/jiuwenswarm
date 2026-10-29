# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit deployment controls for publishing SkillPacks to a compatible Hub."""

import os

from ..skill.skillpack_config import skillpack_productization_enabled
from .asset_publish_adapters import PublishValidationError


def skillpack_publish_profile() -> str:
    """Require a controlled audience until the Hub supports client-version gates."""
    if not skillpack_productization_enabled():
        raise PublishValidationError("SKILLPACK_DISABLED")
    profile = os.getenv("SKILLPACK_HUB_TRANSPORT_PROFILE", "legacy_skill_v1")
    if profile not in {"legacy_skill_v1", "native_skillpack_v1"}:
        raise PublishValidationError("INVALID_SKILLPACK_HUB_PROFILE")
    if os.getenv("SKILLPACK_HUB_DEPLOYMENT_SCOPE", "") not in {"isolated", "version_gated"}:
        raise PublishValidationError("SKILLPACK_MARKET_GATE_REQUIRED")
    return profile
