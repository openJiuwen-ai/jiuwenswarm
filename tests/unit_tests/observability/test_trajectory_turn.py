# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for trajectory turn identity across the traces one turn spans."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from jiuwenswarm.observability.turn import (
    SESSION_STATE_KEY,
    SessionTurnTracker,
    TurnIdentity,
)

test_logger = logging.getLogger("tests.trajectory_turn")


class _FakeSession:
    """Minimal session exposing only the state API the tracker uses."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.committed_state: dict[str, Any] = {}

    def update_state(self, data: dict) -> None:
        """Merge *data* into the session state."""
        self.state.update(data)

    def get_state(self, key: str | None = None) -> Any:
        """Return the value stored under *key*."""
        return self.state.get(key)

    async def commit(self) -> None:
        """Capture the state a rebuilt Session would restore."""
        self.committed_state = dict(self.state)


class _BrokenSession:
    """Session whose state API always fails, exercising the degraded path."""

    def update_state(self, data: dict) -> None:
        """Fail every write."""
        raise RuntimeError("state write unavailable")

    def get_state(self, key: str | None = None) -> Any:
        """Fail every read."""
        raise RuntimeError("state read unavailable")


def test_fresh_request_opens_a_new_turn():
    session = _FakeSession()
    tracker = SessionTurnTracker()

    first = tracker.resolve(session, continues_turn=False)
    second = tracker.resolve(session, continues_turn=False)

    test_logger.info("turns: %s -> %s", first, second)
    assert (first.turn_number, second.turn_number) == (1, 2)
    assert first.turn_id != second.turn_id


def test_hitl_resume_continues_the_turn_in_flight():
    session = _FakeSession()
    tracker = SessionTurnTracker()

    started = tracker.resolve(session, continues_turn=False)
    resumed = tracker.resolve(session, continues_turn=True)
    resumed_again = tracker.resolve(session, continues_turn=True)

    test_logger.info("turn held across resumes: %s", resumed_again)
    assert resumed == started
    assert resumed_again == started


def test_turn_after_a_resume_advances_once():
    session = _FakeSession()
    tracker = SessionTurnTracker()

    tracker.resolve(session, continues_turn=False)
    tracker.resolve(session, continues_turn=True)
    following = tracker.resolve(session, continues_turn=False)

    test_logger.info("turn after resume: %s", following)
    assert following.turn_number == 2


def test_resume_survives_a_tracker_rebuilt_from_session_state():
    # The adapter can be evicted and rebuilt between the question and its
    # answer; the durable copy is what keeps the turn intact.
    session = _FakeSession()
    started = SessionTurnTracker().resolve(session, continues_turn=False)

    resumed = SessionTurnTracker().resolve(session, continues_turn=True)

    test_logger.info("turn restored from session state: %s", resumed)
    assert resumed == started
    assert session.state[SESSION_STATE_KEY] == started.to_dict()


def test_numbering_continues_from_persisted_state():
    session = _FakeSession()
    session.state[SESSION_STATE_KEY] = TurnIdentity("turn-earlier", 7).to_dict()

    resolved = SessionTurnTracker().resolve(session, continues_turn=False)

    test_logger.info("turn resumed numbering: %s", resolved)
    assert resolved.turn_number == 8


def test_resolution_without_a_session_still_yields_a_turn():
    # A brand-new message resolves its turn before the loop has a session.
    tracker = SessionTurnTracker()

    first = tracker.resolve(None, continues_turn=False)
    resumed = tracker.resolve(None, continues_turn=True)

    test_logger.info("session-less turn: %s", first)
    assert first.turn_number == 1
    assert resumed == first


@pytest.mark.asyncio
async def test_sync_checkpoints_a_turn_resolved_before_the_session_existed():
    tracker = SessionTurnTracker()
    resolved = tracker.resolve(None, continues_turn=False)
    session = _FakeSession()

    await tracker.sync(session)

    test_logger.info("persisted after sync: %s", session.state)
    assert session.state[SESSION_STATE_KEY] == resolved.to_dict()
    assert session.committed_state[SESSION_STATE_KEY] == resolved.to_dict()


@pytest.mark.asyncio
async def test_numbering_survives_a_session_rebuilt_from_checkpoint():
    session = _FakeSession()
    tracker = SessionTurnTracker()
    first = tracker.resolve(session, continues_turn=False)
    await tracker.sync(session)

    rebuilt = _FakeSession()
    rebuilt.state = dict(session.committed_state)
    second = SessionTurnTracker().resolve(rebuilt, continues_turn=False)

    assert (first.turn_number, second.turn_number) == (1, 2)


def test_unusable_session_state_degrades_to_a_new_turn():
    tracker = SessionTurnTracker()

    resolved = tracker.resolve(_BrokenSession(), continues_turn=False)

    test_logger.info("degraded turn: %s", resolved)
    assert resolved.turn_number == 1


def test_corrupt_snapshot_is_ignored_rather_than_trusted():
    session = _FakeSession()
    session.state[SESSION_STATE_KEY] = {"turn_id": "", "turn_number": 0}

    resolved = SessionTurnTracker().resolve(session, continues_turn=True)

    test_logger.info("turn from corrupt snapshot: %s", resolved)
    assert resolved.turn_number == 1
    assert resolved.turn_id != ""


def test_identity_round_trips_through_its_serialized_form():
    identity = TurnIdentity("turn-abc", 3)

    restored = TurnIdentity.from_dict(identity.to_dict())

    assert restored == identity
    assert TurnIdentity.from_dict(None) is None
    assert TurnIdentity.from_dict({"turn_number": 2}) is None
