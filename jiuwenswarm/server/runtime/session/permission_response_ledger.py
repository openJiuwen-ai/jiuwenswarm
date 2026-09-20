# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-local deduplication for permission continuation responses."""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass


logger = logging.getLogger(__name__)


_MAX_RECENT_KEYS = 1024
_PermissionResponseKey = tuple[str, str]
_team_permission_resume_landed: ContextVar[bool | None] = ContextVar(
    "team_permission_resume_landed",
    default=None,
)


def reset_team_permission_resume_landed() -> None:
    """Clear the per-task Team InteractiveInput landing flag."""
    _team_permission_resume_landed.set(None)


def mark_team_permission_resume_landed(landed: bool) -> None:
    """Record whether a Team InteractiveInput resume was delivered or queued."""
    _team_permission_resume_landed.set(bool(landed))


def consume_team_permission_resume_landed() -> bool | None:
    """Return and clear the Team InteractiveInput landing flag."""
    landed = _team_permission_resume_landed.get()
    _team_permission_resume_landed.set(None)
    return landed


def settle_permission_reservation(
    reservation: PermissionResponseReservation | None,
    *,
    settle_by_team_landing: bool,
) -> None:
    """Complete a permission click, or abandon it when a Team resume did not land.

    Single-agent permission clicks still complete unconditionally. Team
    InteractiveInput clicks only complete when ``resume_interrupt`` delivered
    or queued the approval; otherwise the key is left retryable.
    """
    if reservation is None:
        return
    if not settle_by_team_landing:
        reservation.complete()
        return
    if consume_team_permission_resume_landed():
        reservation.complete()
        return
    reservation.abandon()
    logger.info(
        "team permission response abandoned because resume did not land: session_id=%s response_id=%s",
        reservation.key[0],
        reservation.key[1],
    )


@dataclass(eq=False)
class PermissionResponseReservation:
    """A single permission response's right to enter the runtime."""

    _ledger: PermissionResponseLedger
    _key: _PermissionResponseKey
    _started: bool = False

    @property
    def key(self) -> _PermissionResponseKey:
        """Return the session-scoped response key."""
        return self._key

    @property
    def started(self) -> bool:
        """Return whether this reservation has entered the runtime."""
        return self._started

    def start(self) -> bool:
        """Claim runtime entry if this reservation is still current."""
        if self._started or not self._ledger.is_current_reservation(self):
            return False
        self._started = True
        return True

    def complete(self) -> None:
        """Remember a response after it has entered the runtime."""
        self._ledger.complete_reservation(self)

    def abandon(self) -> None:
        """Drop a started reservation without remembering it as answered."""
        self._ledger.abandon_reservation(self)

    def release_if_unstarted(self) -> None:
        """Release a queued reservation so a retry can replace it."""
        self._ledger.release_if_unstarted(self)


class PermissionResponseLedger:
    """Track active and recently executed permission responses."""

    def __init__(self, *, max_recent_keys: int = _MAX_RECENT_KEYS) -> None:
        if max_recent_keys < 0:
            raise ValueError("max_recent_keys must be non-negative")
        self._max_recent_keys = max_recent_keys
        self._active: dict[
            _PermissionResponseKey, PermissionResponseReservation
        ] = {}
        self._recent: OrderedDict[_PermissionResponseKey, None] = OrderedDict()

    def reserve(
        self,
        session_id: str,
        response_id: str,
    ) -> PermissionResponseReservation | None:
        """Reserve an opaque response ID once for a session."""
        key = (session_id, response_id)
        if key in self._active or key in self._recent:
            return None
        reservation = PermissionResponseReservation(self, key)
        self._active[key] = reservation
        return reservation

    def is_current_reservation(
        self,
        reservation: PermissionResponseReservation,
    ) -> bool:
        """Return whether a reservation is still the current active entry."""
        return self._active.get(reservation.key) is reservation

    def complete_reservation(
        self,
        reservation: PermissionResponseReservation,
    ) -> None:
        """Complete a reservation and remember its key if it was started."""
        if self._active.get(reservation.key) is not reservation:
            return
        self._active.pop(reservation.key, None)
        if not reservation.started or self._max_recent_keys == 0:
            return
        self._recent[reservation.key] = None
        while len(self._recent) > self._max_recent_keys:
            self._recent.popitem(last=False)

    def abandon_reservation(
        self,
        reservation: PermissionResponseReservation,
    ) -> None:
        """Drop an active reservation without recording it as recently answered."""
        if self._active.get(reservation.key) is not reservation:
            return
        self._active.pop(reservation.key, None)

    def release_if_unstarted(
        self,
        reservation: PermissionResponseReservation,
    ) -> None:
        """Release a reservation only if it has not started."""
        if (
            not reservation.started
            and self._active.get(reservation.key) is reservation
        ):
            self._active.pop(reservation.key, None)
