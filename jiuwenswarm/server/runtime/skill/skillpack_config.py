# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Conservative rollout switch shared by SkillPack product entry points."""

import os


def skillpack_productization_enabled() -> bool:
    return os.getenv("SKILLPACK_PRODUCTIZATION_ENABLED", "false").strip().lower() == "true"
