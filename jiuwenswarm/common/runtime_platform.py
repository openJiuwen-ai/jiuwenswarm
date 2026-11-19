# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared platform detection for sandboxed runtimes."""

from __future__ import annotations

import os
import sys


def is_harmony_runtime() -> bool:
    """Detect HarmonyOS even when its Python reports a Linux platform.

    Prefer the explicit runtime marker and native platform identifier. HNP
    executable paths preserve the existing sandbox detection fallback.
    """
    if os.environ.get("JIUWEN_HARMONY_RUNTIME") == "1":
        return True
    if sys.platform == "ohos":
        return True
    return any(
        "/data/app/" in entry and "/hnp/" in entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
    )
