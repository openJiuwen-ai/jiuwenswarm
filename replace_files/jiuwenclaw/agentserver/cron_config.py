# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Cron feature switches shared by agentserver modules."""

from __future__ import annotations

import os


def should_register_cron_tools() -> bool:
    """Return False when JiuwenClaw cron tools are disabled by env."""
    return os.getenv("JIUWENCLAW_DISABLE_CRON_TOOLS") != "1"

