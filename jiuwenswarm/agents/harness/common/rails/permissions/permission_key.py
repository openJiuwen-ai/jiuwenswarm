# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side permission Manager key for personal and enterprise editions.

This is the **only** place business code should turn identity dims into a
Manager key before ``check_permission`` / rail registration.  Callers must not
re-implement triad rules or spray ``try/except`` around ``make_key``.

Personal: always ``default``.
Enterprise: full triad → ``group:/bot:/user:``; missing or partial triad →
``default`` + warning (IM/cron/单聊 often lack a complete routing triple;
failing the whole request here is wrong).
"""

from __future__ import annotations

import logging

from openjiuwen.harness.security.permission_key import DEFAULT_KEY, make_key

from jiuwenswarm.edition import is_enterprise

__all__ = [
    "DEFAULT_KEY",
    "resolve_permission_key",
]

logger = logging.getLogger(__name__)


def resolve_permission_key(
    *,
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Return the Manager key for the current edition.

    Personal: ignores identity fields and returns ``default``.
    Enterprise: complete triad → ``make_key(...)``; empty or partial triad →
    ``DEFAULT_KEY`` with a warning (never raises for identity shape).
    """
    if not is_enterprise():
        return make_key()
    # make_key() with all-None yields default silently; log so ops can see
    # enterprise requests that never carried routing identity.
    if group_id is None and bot_id is None and user_id is None:
        logger.warning(
            "enterprise permission key: empty triad; fallback to %s",
            DEFAULT_KEY,
        )
        return DEFAULT_KEY
    try:
        return make_key(group_id=group_id, bot_id=bot_id, user_id=user_id)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "enterprise permission key incomplete "
            "(group_id=%r bot_id=%r user_id=%r): %s; fallback to %s",
            group_id,
            bot_id,
            user_id,
            exc,
            DEFAULT_KEY,
        )
        return DEFAULT_KEY
