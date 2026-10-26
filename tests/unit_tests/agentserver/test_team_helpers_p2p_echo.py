# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for P2P sender-window echo in team fan_out and /join history.

Covers two aligned changes that make a ``team.message.p2p`` card visible in the
*sender's* window as well as the recipient's:

1. ``team_helpers._p2p_fanout`` appends a ``private`` target for ``from_member``
   so the sender's /join window receives its own P2P card.
2. ``session_history._is_member_relevant`` treats a p2p record as relevant to
   both ``to_member`` and ``from_member`` so the /join history replay matches the
   live fan_out semantics.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.session import session_history


def _p2p_event(*, from_member: str, to_member: str) -> dict[str, Any]:
    """Build a ``team.message`` event wrapping a p2p inner payload."""
    return {
        "event_type": "team.message",
        "session_id": "sess-p2p",
        "event": {
            "type": "team.message.p2p",
            "team_id": "demo-team",
            "from_member": from_member,
            "to_member": to_member,
            "message_id": "msg-1",
            "content": "hi",
        },
    }


def _broadcast_event(*, from_member: str | None = None) -> dict[str, Any]:
    """Build a ``team.message`` event wrapping a broadcast inner payload."""
    return {
        "event_type": "team.message",
        "session_id": "sess-broadcast",
        "event": {
            "type": "team.message.broadcast",
            "team_id": "demo-team",
            "from_member": from_member,
            "message_id": "msg-2",
            "content": "hello all",
        },
    }


def _target_summary(targets: list[dict]) -> list[tuple[str, tuple[str, ...], str | None]]:
    """Reduce LogicalTarget list to (intent, member_names, speaker) tuples."""
    summaries: list[tuple[str, tuple[str, ...], str | None]] = []
    for tgt in targets:
        names = tuple(tgt.get("member_names") or [])
        summaries.append((tgt["intent"], names, tgt.get("speaker")))
    return summaries


def test_p2p_fanout_echoes_sender_via_private():
    """p2p (from=reviewer-2, to=reviewer-1) → godview / mention(to) / private(from)."""
    event = _p2p_event(from_member="reviewer-2", to_member="reviewer-1")

    targets = team_helpers._build_logical_targets(event)

    assert _target_summary(targets) == [
        ("godview", (), None),
        ("mention", ("reviewer-1",), "reviewer-2"),
        ("private", ("reviewer-2",), "reviewer-2"),
    ]
    # The sender target must be private (no @ self) — mention carries the
    # recipient; broadcast-like mention_all must not be set on any target.
    assert not any(t.get("mention_all") for t in targets)


def test_p2p_fanout_order_is_godview_then_recipient_then_sender():
    """godview first, recipient (mention) second, sender (private) last.

    Order matters for same-container dedup: the recipient intent is dispatched
    before the sender's private intent so that, when both share one physical
    container, the recipient view wins and the sender's private is deduped to a
    no-op rather than the other way around.
    """
    event = _p2p_event(from_member="reviewer-2", to_member="reviewer-1")

    targets = team_helpers._build_logical_targets(event)
    intents = [t["intent"] for t in targets]

    assert intents == ["godview", "mention", "private"]
    assert targets[-1]["member_names"] == ["reviewer-2"]


def test_p2p_fanout_skips_private_when_from_member_missing():
    """Missing from_member must not produce a private([None]) target."""
    inner = {
        "type": "team.message.p2p",
        "team_id": "demo-team",
        "to_member": "reviewer-1",
        # from_member intentionally absent
    }

    targets = team_helpers._p2p_fanout(inner)

    assert _target_summary(targets) == [
        ("godview", (), None),
        ("mention", ("reviewer-1",), None),
    ]
    # No target should carry a None member name.
    assert all(None not in (t.get("member_names") or []) for t in targets)


def test_p2p_fanout_skips_private_when_from_member_is_falsy():
    """Empty-string from_member is treated as missing."""
    inner = {
        "type": "team.message.p2p",
        "team_id": "demo-team",
        "from_member": "",
        "to_member": "reviewer-1",
    }

    targets = team_helpers._p2p_fanout(inner)

    assert len(targets) == 2
    assert all(t["intent"] != "private" for t in targets)


def test_broadcast_fanout_unchanged_by_p2p_echo_change():
    """broadcast fan_out stays [godview, mention_all] — sender echo is p2p-only."""
    event = _broadcast_event(from_member="reviewer-1")

    targets = team_helpers._build_logical_targets(event)

    assert len(targets) == 2
    assert targets[0]["intent"] == "godview"
    assert targets[1]["intent"] == "mention"
    assert targets[1].get("mention_all") is True
    assert targets[1].get("member_names") == []
    assert targets[1].get("speaker") == "reviewer-1"
    # No private target for broadcast.
    assert not any(t["intent"] == "private" for t in targets)


# ---------------------------------------------------------------------------
# session_history._is_member_relevant
# ---------------------------------------------------------------------------


def _history_item(msg_type: str, *, from_member: str = "", to_member: str = "") -> dict[str, Any]:
    """Build a flat history item mirroring append_history_record's extra spread.

    ``append_history_record`` flattens the event inner dict onto the item top
    level via ``item.update(serialized_extra)``, so ``from_member``/``to_member``
    are present both at the item top level and inside ``event``.
    """
    return {
        "event_type": "team.message",
        "type": msg_type,
        "from_member": from_member,
        "to_member": to_member,
        "event": {
            "type": msg_type,
            "from_member": from_member,
            "to_member": to_member,
        },
    }


def test_is_member_relevant_p2p_visible_to_recipient_and_sender():
    item = _history_item("team.message.p2p", from_member="reviewer-2", to_member="reviewer-1")

    assert session_history._is_member_relevant(item, "reviewer-1") is True  # recipient
    assert session_history._is_member_relevant(item, "reviewer-2") is True  # sender
    assert session_history._is_member_relevant(item, "reviewer-3") is False  # bystander


def test_is_member_relevant_p2p_uses_inner_fields_when_top_level_absent():
    """When only the nested ``event`` carries member names, relevance still holds."""
    item = {
        "event_type": "team.message",
        "event": {
            "type": "team.message.p2p",
            "from_member": "reviewer-2",
            "to_member": "reviewer-1",
        },
    }

    assert session_history._is_member_relevant(item, "reviewer-1") is True
    assert session_history._is_member_relevant(item, "reviewer-2") is True
    assert session_history._is_member_relevant(item, "reviewer-3") is False


def test_is_member_relevant_broadcast_visible_to_all():
    item = _history_item("team.message.broadcast", from_member="reviewer-1")

    assert session_history._is_member_relevant(item, "reviewer-1") is True
    assert session_history._is_member_relevant(item, "reviewer-2") is True
    assert session_history._is_member_relevant(item, "reviewer-3") is True


def test_is_member_relevant_bystander_teammate_output_only_for_self():
    """chat.* teammate output stays visible only to the member who owns it."""
    item = {
        "event_type": "chat.final",
        "role": "teammate",
        "member_name": "reviewer-2",
    }

    assert session_history._is_member_relevant(item, "reviewer-2") is True
    assert session_history._is_member_relevant(item, "reviewer-1") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
