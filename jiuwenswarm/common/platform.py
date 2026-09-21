# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime platform and capability checks used by server entrypoints.

Ported from the harmony_dev branch (jiuwenclaw.runtime.platform) as part of
the HarmonyOS HNP deployment support. The canonical environment variable is
``JIUWENSWARM_RUNTIME_PLATFORM``; the legacy ``JIUWENCLAW_RUNTIME_PLATFORM``
is still honoured so existing on-device launchers keep working.
"""

from __future__ import annotations

import os
import sys


RUNTIME_PLATFORM_ENV = "JIUWENSWARM_RUNTIME_PLATFORM"
LEGACY_RUNTIME_PLATFORM_ENV = "JIUWENCLAW_RUNTIME_PLATFORM"
OHOS_PLATFORM = "ohos"


def runtime_platform() -> str:
    """Return an explicit platform, with a conservative HNP fallback."""

    explicit = os.environ.get(RUNTIME_PLATFORM_ENV, "").strip().lower()
    if not explicit:
        explicit = os.environ.get(LEGACY_RUNTIME_PLATFORM_ENV, "").strip().lower()
    if explicit:
        return explicit
    if sys.platform == OHOS_PLATFORM:
        return OHOS_PLATFORM
    if sys.platform.startswith("linux") and os.path.isdir("/data/service/hnp"):
        return OHOS_PLATFORM
    return sys.platform.lower()


def is_ohos_runtime() -> bool:
    return runtime_platform() == OHOS_PLATFORM


def sandbox_supported() -> bool:
    """Whether the current runtime has a supported JiuwenBox host platform."""

    if is_ohos_runtime():
        return False
    return sys.platform in ("linux", "win32")


__all__ = [
    "OHOS_PLATFORM",
    "RUNTIME_PLATFORM_ENV",
    "LEGACY_RUNTIME_PLATFORM_ENV",
    "is_ohos_runtime",
    "runtime_platform",
    "sandbox_supported",
]
