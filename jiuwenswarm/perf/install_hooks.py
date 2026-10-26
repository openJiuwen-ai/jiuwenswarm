# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Install swarm-side perf hooks (DeepAdapter is wired separately).

Parent request boundaries live in ``interface_deep`` / ``interface_code``.
This installer patches ``DeepAgent.create_subagent`` so TaskTool children also
get ``RequestSummaryRail(record_only=True)``.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.perf.subagent_hooks import apply_create_subagent_perf_patch


def install_perf_hooks(_adapter: Any = None) -> None:
    """Apply create_subagent RequestSummaryRail patch (idempotent)."""
    apply_create_subagent_perf_patch()
