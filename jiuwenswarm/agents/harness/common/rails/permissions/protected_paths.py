# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Central protected path defaults for JiuwenClaw auto permission."""

from __future__ import annotations

JIUWENCLAW_PROTECTED_WRITE_PATHS = (
    "jiuwenbox",
    "jiuwenswarm/agents/harness/common/rails/permissions",
    "tests/unit_tests/agentserver/permissions",
    "tests/system_tests/auto_permission_matrix",
)


def merge_protected_write_paths(*path_groups: tuple[str, ...]) -> tuple[str, ...]:
    """Return a stable de-duplicated protected path tuple."""
    merged: list[str] = []
    seen: set[str] = set()
    for path_group in path_groups:
        for path in path_group:
            text = str(path).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return tuple(merged)
