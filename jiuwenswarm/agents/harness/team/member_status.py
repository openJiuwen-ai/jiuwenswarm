# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team 成员名册口径.

后端对前端有多个名册出口（``team.snapshot``、roster announce），它们必须对
"谁还在名册上"用同一个判断，否则一处过滤、一处不过滤，前端就会把已离队成员
重新画回面板。
"""

from __future__ import annotations

from openjiuwen.agent_teams.schema.status import MemberStatus


def is_departed_member(status: str | None) -> bool:
    """Whether a member has left the roster for good (``shut_down``).

    ``dormant`` is not departed: it stays on the roster and a mention wakes it.
    ``shutdown_requested`` is not departed either: it may still settle into
    ``dormant``.
    """
    return str(status or "").strip().lower() == MemberStatus.SHUTDOWN.value
