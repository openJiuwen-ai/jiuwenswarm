# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build-edition gate for methods shared by both editions' wire enum.

``ReqMethod`` carries the enterprise team control plane in every build, but
only enterprise builds register those handlers. Registration alone is not a
request-boundary check: an unregistered method used to fall through to the chat
branch. This module gives the server ingress one explicit answer for
"declared, but not served by this edition".
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.schema.message import ReqMethod

#: Team control-plane methods served only by enterprise builds. Local team
#: methods (team.list / team.snapshot / team.delete / ...) exist in both
#: editions and must not be listed here.
ENTERPRISE_ONLY_METHODS: frozenset[str] = frozenset({
    ReqMethod.TEAM_CREATE.value,
    ReqMethod.TEAM_GET.value,
    ReqMethod.TEAM_STOP.value,
    ReqMethod.TEAM_MEMBER_JOIN.value,
    ReqMethod.TEAM_MEMBER_LEAVE.value,
    ReqMethod.TEAM_MEMBERS_LIST.value,
    ReqMethod.TEAM_QUOTA_GET.value,
})


def method_name(method: Any) -> str:
    """Normalize a ``ReqMethod`` member or raw wire string to a method name."""
    name = getattr(method, "value", method)
    return name if isinstance(name, str) else ""


def enterprise_only_denial(method: Any) -> str | None:
    """Return the refusal reason, or ``None`` when the method may proceed."""
    name = method_name(method)
    if name not in ENTERPRISE_ONLY_METHODS:
        return None
    from jiuwenswarm.enterprise.edition import is_enterprise

    if is_enterprise():
        return None
    return f"method not available in personal edition: {name}"
