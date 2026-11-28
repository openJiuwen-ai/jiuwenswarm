# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Environment merge shared by process runtimes."""

import os


def build_process_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Merge service and request environments and remove the management token."""
    environment = {**os.environ, **(extra or {})}
    environment.pop("JIUWENBOX_API_TOKEN", None)
    return environment
