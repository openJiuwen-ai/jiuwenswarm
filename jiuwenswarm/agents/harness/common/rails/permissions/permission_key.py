# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve the permission-manager key for this process (personal and enterprise).

One call path for both editions: only the arguments to ``make_key`` differ.
Personal edition always uses ``make_key()`` → ``default``. Enterprise edition
requires the full ``group`` / ``bot`` / ``user`` triad and passes them through.
"""

from __future__ import annotations

from openjiuwen.harness.security.permission_key import DEFAULT_KEY, make_key

from jiuwenswarm.edition import is_enterprise

__all__ = [
    "DEFAULT_KEY",
    "resolve_permission_key",
]


def resolve_permission_key(
    *,
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Return the manager key for the current edition.

    Personal: ignores identity fields and returns ``default``.
    Enterprise: requires ``group_id``, ``bot_id`` and ``user_id`` together
    (same rules as :func:`make_key`); missing or partial identity raises
    ``ValueError``.  Note ``make_key()`` with no args would yield ``default``,
    so enterprise must reject the empty triad here before calling it.
    """
    if not is_enterprise():
        return make_key()
    if group_id is None and bot_id is None and user_id is None:
        raise ValueError(
            "enterprise permission key requires group_id, bot_id and user_id"
        )
    return make_key(group_id=group_id, bot_id=bot_id, user_id=user_id)
