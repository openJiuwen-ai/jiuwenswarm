# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trajectory turn identity for a session.

A turn is one complete ReAct loop: it opens when the loop starts and closes
when the loop emits its final text answer. User input arriving mid-loop opens
no turn of its own as long as the loop survives it — a HITL resume
(``ask_user`` / permission / confirm) answers a question the agent itself
asked and the loop picks up from where it blocked, and a steer is folded into
the round in progress.

Every other inbound request opens a turn, including the ones that carry user
text into a busy session: ``supplement`` and ``cancel`` abandon the running
loop (dropping whatever step had not finished) before the new message runs, so
the loop they interrupt never reached its final answer and the message that
follows starts a loop of its own.

Because a HITL resume runs in a fresh trace, a turn outlives the trace that
started it. The identity therefore lives on the session rather than on any one
root span, and ``openjiuwen.turn.id`` is what stitches the traces of a single
turn back together downstream.

Deciding whether a request continues the loop takes runtime state this module
does not own, so the caller makes that call and passes it in; this module only
keeps the identity that follows from it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Session-state key holding the serialized turn identity. Namespaced so it
# cannot collide with the harness's own ``deepagent`` state blob.
SESSION_STATE_KEY = "trajectory_turn"

_TURN_ID_FIELD = "turn_id"
_TURN_NUMBER_FIELD = "turn_number"


@dataclass(frozen=True)
class TurnIdentity:
    """Identity of one trajectory turn.

    Attributes:
        turn_id: Stable identity shared by every trace carrying part of this
            turn. A HITL resume reuses it across the trace boundary.
        turn_number: 1-based turn number within the session.
    """

    turn_id: str
    turn_number: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict for session state.

        Returns:
            Dict with the turn id and turn number.
        """
        return {
            _TURN_ID_FIELD: self.turn_id,
            _TURN_NUMBER_FIELD: self.turn_number,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TurnIdentity | None":
        """Restore from a dict previously produced by :meth:`to_dict`.

        Args:
            data: Serialized identity; anything malformed reads as absent so a
                corrupt snapshot degrades to "start a new turn" instead of
                failing the request.

        Returns:
            The restored identity, or None when *data* holds no usable one.
        """
        if not isinstance(data, dict):
            return None
        turn_id = str(data.get(_TURN_ID_FIELD) or "").strip()
        raw_number = data.get(_TURN_NUMBER_FIELD)
        if not turn_id or not isinstance(raw_number, int) or raw_number < 1:
            return None
        return cls(turn_id=turn_id, turn_number=raw_number)


class SessionTurnTracker:
    """Resolve which trajectory turn an inbound request belongs to.

    One tracker per session-scoped adapter. The in-memory copy is what serves
    a HITL resume: the adapter stays alive while the agent blocks on the
    question, so the identity is always at hand. The session-state copy is the
    durable one, covering a resume that outlives the adapter — a reloaded
    session, or a rewind that replays a turn.
    """

    def __init__(self) -> None:
        self._current: TurnIdentity | None = None

    @property
    def current(self) -> TurnIdentity | None:
        """Return the turn resolved most recently, if any."""
        return self._current

    def resolve(self, session: Any | None, *, continues_turn: bool) -> TurnIdentity:
        """Resolve the turn for a request that is about to open a root span.

        Args:
            session: Live session used to read and persist the identity;
                None resolves from memory alone, which still covers a resume
                inside one adapter lifetime.
            continues_turn: Whether this request joins the ReAct loop already
                running rather than starting one — a HITL resume, or a steer
                folded into the round in progress. The caller owns that
                decision; this type only keeps the identity it implies.

        Returns:
            The turn to stamp on this request's root span.
        """
        known = self._current or self._load(session)
        if continues_turn and known is not None:
            # The loop that asked is still the loop that runs — same turn,
            # new trace. Re-stage so ``sync`` can checkpoint the memory copy
            # even when this tracker resolved it without reading the Session.
            self._current = known
            self._persist(session, known)
            return known
        previous_number = known.turn_number if known is not None else 0
        resolved = TurnIdentity(turn_id=uuid.uuid4().hex, turn_number=previous_number + 1)
        self._current = resolved
        self._persist(session, resolved)
        return resolved

    async def sync(self, session: Any | None) -> None:
        """Persist and checkpoint the resolved turn once *session* is available.

        A brand-new message resolves its turn before the session exists, so the
        write at resolve time is a no-op. Calling this after the run has a
        session closes that gap. ``update_state`` only mutates the live Session;
        ``commit`` is required for a rebuilt adapter to recover the counter.

        Args:
            session: Live session to persist into; None is a no-op.
        """
        if self._current is None:
            return
        if not self._persist(session, self._current):
            return
        try:
            await session.commit()
        except Exception as exc:
            logger.debug("[Trajectory] turn state commit failed: %s", exc)

    @staticmethod
    def _load(session: Any | None) -> TurnIdentity | None:
        """Read the persisted identity, treating any failure as absent."""
        if session is None:
            return None
        try:
            return TurnIdentity.from_dict(session.get_state(SESSION_STATE_KEY))
        except Exception as exc:
            logger.debug("[Trajectory] turn state read failed: %s", exc)
            return None

    @staticmethod
    def _persist(session: Any | None, identity: TurnIdentity) -> bool:
        """Stage the identity in session state, best-effort.

        Returns whether the state was staged. Failing the request over an
        observability counter would cost the whole run, so this never raises.
        """
        if session is None:
            return False
        try:
            session.update_state({SESSION_STATE_KEY: identity.to_dict()})
            return True
        except Exception as exc:
            logger.debug("[Trajectory] turn state write failed: %s", exc)
            return False
