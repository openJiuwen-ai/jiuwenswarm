# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility retry for OpenJiuwen team-member execution-status writes."""

from __future__ import annotations

import inspect
import logging
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)

_installed = False
_original_update_member_execution_status: Any = None
_installed_wrapper: Any = None


def install_team_db_lock_retry_patch() -> bool:
    """Retry transient SQLite locks around SDK execution-status updates."""
    global _installed, _original_update_member_execution_status, _installed_wrapper
    if _installed:
        return True

    try:
        from openjiuwen.agent_teams.tools.database.member_dao import MemberDao
    except ImportError as exc:
        logger.warning("[TeamDBLockPatch] skip: cannot import MemberDao: %s", exc)
        return False

    original = getattr(MemberDao, "update_member_execution_status", None)
    if not callable(original):
        logger.warning("[TeamDBLockPatch] skip: incompatible MemberDao implementation")
        return False

    if original is _installed_wrapper and _installed_wrapper is not None:
        _original_update_member_execution_status = None
        _installed = True
        return True

    try:
        src = inspect.getsource(original)
    except (OSError, TypeError):
        src = ""
    if "retry_on_locked" in src or "retry_on_transient" in src:
        _installed = True
        return True

    try:
        from openjiuwen.agent_teams.tools.database.engine import retry_on_transient
    except ImportError as exc:
        logger.warning("[TeamDBLockPatch] skip: SDK retry helper unavailable: %s", exc)
        return False

    @wraps(original)
    async def update_member_execution_status(
        self: Any,
        member_name: str,
        team_name: str,
        execution_status: str,
    ) -> Any:
        async def op() -> Any:
            return await original(self, member_name, team_name, execution_status)

        return await retry_on_transient(
            op,
            label=f"update_member_execution_status {member_name}",
        )

    try:
        MemberDao.update_member_execution_status = update_member_execution_status
    except (AttributeError, TypeError):
        logger.warning("[TeamDBLockPatch] skip: cannot replace MemberDao method", exc_info=True)
        return False
    _original_update_member_execution_status = original
    _installed_wrapper = update_member_execution_status
    _installed = True
    logger.info("[TeamDBLockPatch] MemberDao execution-status lock retry patch applied")
    return True


def reset_team_db_lock_retry_patch() -> None:
    """Restore the SDK method (tests only)."""
    global _installed, _original_update_member_execution_status, _installed_wrapper
    if not _installed:
        return
    try:
        from openjiuwen.agent_teams.tools.database.member_dao import MemberDao

        current = getattr(MemberDao, "update_member_execution_status", None)
        if _original_update_member_execution_status is not None:
            if current is _installed_wrapper:
                MemberDao.update_member_execution_status = _original_update_member_execution_status
            else:
                logger.warning(
                    "[TeamDBLockPatch] skip restore: MemberDao method changed after patch installation"
                )
    except ImportError as exc:
        logger.warning("[TeamDBLockPatch] cannot restore MemberDao method: %s", exc, exc_info=True)
    except (AttributeError, TypeError):
        logger.warning("[TeamDBLockPatch] cannot restore MemberDao method", exc_info=True)
    finally:
        _original_update_member_execution_status = None
        _installed_wrapper = None
        _installed = False


__all__ = ["install_team_db_lock_retry_patch", "reset_team_db_lock_retry_patch"]
