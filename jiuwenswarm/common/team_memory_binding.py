# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-owned stable Team coordinates; chat sessions and members are not keys."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from urllib.parse import quote

from agent_gateway.common.mode_matrix import deprecate_mode, is_team_mode
from openjiuwen.core.common.run_context import RunContext, RunContextInput

from jiuwenswarm.common.memory_config import get_builtin_memory_provider


def uses_jiuwen_team_memory(config: dict, mode: str) -> bool:
    normalized = deprecate_mode(mode)
    return (get_builtin_memory_provider(config) == "jiuwen" and is_team_mode(normalized)
            and not normalized.startswith("team.design"))


def team_memory_key(org: str, space: str, user: str, expert_group: str = "") -> str:
    group = expert_group.strip()
    payload = json.dumps([org, space, user, group], ensure_ascii=False)
    prefix = "team" if group else "main_team"
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def expert_memory_subject(group: str | None, template: str) -> str:
    return quote(f"{group or 'expert'}:{template}", safe=":._-")


def resolve_expert_memory_subject(group, names, member, reference, role: str = "") -> str | None:
    template = reference.get("name")
    if template:
        return expert_memory_subject(reference.get("group"), template)
    if group and member in names:
        return expert_memory_subject(group, member)
    normalized_role = str(role or "").strip().lower()
    if group and normalized_role == "leader" and "leader" in names:
        return expert_memory_subject(group, "leader")
    return None


def team_memory_user_context(run_context: RunContextInput | None, text: str) -> RunContextInput:
    """Attach a user statement to the invocation context at the trusted host boundary."""
    if isinstance(run_context, RunContext):
        return replace(run_context, extra={
            **run_context.extra, "raw_query": text, "raw_query_origin": "human",
        })
    context = dict(run_context or {})
    extra = dict(context.get("extra") or {})
    extra.update(raw_query=text, raw_query_origin="human")
    context["extra"] = extra
    return context
