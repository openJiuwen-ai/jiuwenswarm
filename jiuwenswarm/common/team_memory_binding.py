# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-owned stable Team coordinates; chat sessions and members are not keys."""
from __future__ import annotations

import hashlib
import json

from agent_gateway.common.mode_matrix import deprecate_mode, is_team_mode

from jiuwenswarm.common.memory_config import get_builtin_memory_provider


def uses_jiuwen_team_memory(config: dict, mode: str) -> bool:
    normalized = deprecate_mode(mode)
    return (get_builtin_memory_provider(config) == "jiuwen" and is_team_mode(normalized)
            and not normalized.startswith("team.design"))


def team_memory_key(org: str, user: str, project: str, expert_group: str = "") -> str:
    payload = json.dumps([org, user, project, expert_group.strip()], ensure_ascii=False)
    return f"{project}--{hashlib.sha256(payload.encode()).hexdigest()[:24]}"
