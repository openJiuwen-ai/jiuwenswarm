# Gateway 侧副本，源文件 jiuwenswarm/common/agentos_runtime.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
"""Trusted deployment context for AgentOS-only Runtime behavior."""

from __future__ import annotations

import os

RUNTIME_PROFILE_ENV = "JIUWENSWARM_RUNTIME_PROFILE"


def is_agentos_runtime() -> bool:
    return os.getenv(RUNTIME_PROFILE_ENV, "").strip().lower() == "agentos"
